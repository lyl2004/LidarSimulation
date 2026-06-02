using TransitionMatrices
using LinearAlgebra
using Statistics

const UM2_TO_M2 = 1e-12

function trapz(y::AbstractVector{T}, x::AbstractVector{T}) where {T<:Real}
    length(y) == length(x) || error("trapz: length mismatch")
    s = zero(T)
    @inbounds for i in 2:length(x)
        s += (y[i] + y[i - 1]) * (x[i] - x[i - 1])
    end
    return s / 2
end

function generate_adaptive_angles(; num_total::Int = 600,
                                    forward_res::Float64 = 0.01,
                                    forward_max::Float64 = 2.0)
    fwd = collect(0.0:forward_res:forward_max)
    !isempty(fwd) && fwd[end] > forward_max && pop!(fwd)
    rest = collect(range(forward_max, 180.0; length = max(2, num_total - length(fwd))))
    return unique(sort(vcat(fwd, rest[2:end])))
end

function resolve_nmax(r_um::Float64, wavelength_um::Float64;
                      nmax_override::Int = 0, nmax_cap::Int = 60)
    nmax_override > 0 && return clamp(nmax_override, 1, nmax_cap)
    x = 2pi * r_um / wavelength_um
    auto = round(Int, x + 4 * x^(1 / 3) + 2)
    return clamp(max(4, auto), 1, nmax_cap)
end

function make_particle(shape_type::String,
                        radius_um::Float64,
                        m::ComplexF64;
                        r_eff::Float64 = 0.0,
                        axis_ratio::Float64 = 1.0)
    r = r_eff > 0.0 ? r_eff : radius_um
    st = lowercase(strip(shape_type))
    if st == "spheroid"
        return Spheroid(r, r * axis_ratio, m)
    elseif st == "sphere"
        return Spheroid(r, r, m)
    elseif st == "cylinder"
        return Cylinder(r, 2.0 * r * axis_ratio, m)
    end
    error("unsupported shape_type for current 11-figure workflow: $shape_type")
end

function normalize_solver_choice(solver_choice)::String
    solver = lowercase(strip(String(solver_choice)))
    return solver == "iitm_only" ? solver : "iitm_only"
end

function build_solver_diagnostics(; solver_requested::String,
                                  solver_used::String,
                                  attempted_solvers::Vector{String},
                                  nmax::Int,
                                  Nr::Int = 0,
                                  Ntheta::Int = 0)
    return Dict{String, Any}(
        "solver_requested" => solver_requested,
        "solver_used" => solver_used,
        "solver_path" => join(attempted_solvers, " -> "),
        "nmax" => nmax,
        "Nr" => Nr,
        "Ntheta" => Ntheta,
    )
end

function validate_tm_result(sigma_ext::Float64, sigma_sca::Float64, g_val::Float64,
                            angles_deg::Vector{Float64}, M11::Vector{Float64},
                            M12::Vector{Float64}, M33::Vector{Float64},
                            M34::Vector{Float64})
    reasons = String[]
    (!isfinite(sigma_ext) || sigma_ext <= 0) && push!(reasons, "sigma_ext_invalid")
    (!isfinite(sigma_sca) || sigma_sca < 0) && push!(reasons, "sigma_sca_invalid")

    omega0 = sigma_ext > 0 ? sigma_sca / sigma_ext : NaN
    (!isfinite(omega0) || omega0 < -1e-8 || omega0 > 1.0 + 1e-6) && push!(reasons, "omega0_out_of_range")
    (!isfinite(g_val) || abs(g_val) > 1.0 + 1e-6) && push!(reasons, "g_out_of_range")

    for (name, arr) in [("M11", M11), ("M12", M12), ("M33", M33), ("M34", M34)]
        any(x -> !isfinite(x), arr) && push!(reasons, "$(name)_nonfinite")
    end

    if isempty(M11) || maximum(abs.(M11)) <= 1e-14
        push!(reasons, "M11_degenerate")
    else
        min_ratio = minimum(M11) / max(maximum(abs.(M11)), 1e-12)
        min_ratio < -1e-3 && push!(reasons, "M11_negative")
    end

    theta_r = deg2rad.(angles_deg)
    norm_val = trapz(max.(M11, 0.0) .* sin.(theta_r), theta_r)
    (!isfinite(norm_val) || norm_val <= 1e-12) && push!(reasons, "phase_normalization_failed")
    return isempty(reasons), join(unique(reasons), "; ")
end

function extract_scatter_fields(T_matrix, lambda_um::Float64, angles_deg::Vector{Float64})
    F = scattering_matrix(T_matrix, lambda_um, angles_deg)
    M11 = Float64.(real.(F[:, 1]))
    M12 = Float64.(real.(F[:, 2]))
    M33 = Float64.(real.(F[:, 4]))
    M34 = Float64.(real.(F[:, 5]))
    return M11, M12, M33, M34
end

function finalize_single_particle_result(T_matrix, lambda_um::Float64, shape_type::String,
                                         solver_requested::String, solver_used::String,
                                         diagnostics::Dict{String, Any},
                                         angles_deg::Vector{Float64})
    Csca_um2 = scattering_cross_section(T_matrix, lambda_um)
    Cext_um2 = extinction_cross_section(T_matrix, lambda_um)
    g_val = asymmetry_parameter(T_matrix, lambda_um)

    sigma_sca = Float64(real(Csca_um2)) * UM2_TO_M2
    sigma_ext = Float64(real(Cext_um2)) * UM2_TO_M2
    g_float = Float64(real(g_val))

    M11, M12, M33, M34 = extract_scatter_fields(T_matrix, lambda_um, angles_deg)
    ok, reason = validate_tm_result(sigma_ext, sigma_sca, g_float, angles_deg, M11, M12, M33, M34)
    ok || error("$(solver_used) result invalid: $reason")

    return (
        sigma_ext = sigma_ext,
        sigma_sca = sigma_sca,
        g = g_float,
        T_matrix = T_matrix,
        lambda_um = lambda_um,
        shape_name = shape_type,
        solver_used = solver_used,
        diagnostics = diagnostics,
        M11 = M11,
        M12 = M12,
        M33 = M33,
        M34 = M34,
    )
end

function single_particle_iitm(radius_um::Float64,
                              m::ComplexF64,
                              wavelength_m::Float64;
                              shape_type::String = "spheroid",
                              r_eff::Float64 = 0.0,
                              axis_ratio::Float64 = 1.0,
                              Nr::Int = 64,
                              Ntheta::Int = 96,
                              nmax_override::Int = 0,
                              angles_deg::Vector{Float64} = generate_adaptive_angles(),
                              solver_requested::String = "iitm_only",
                              attempted_solvers::Vector{String} = ["iitm"])
    lambda_um = wavelength_m * 1e6
    r_calc = r_eff > 0.0 ? r_eff : radius_um
    nmax = resolve_nmax(r_calc, lambda_um; nmax_override = nmax_override)
    particle = make_particle(shape_type, radius_um, m; r_eff = r_eff, axis_ratio = axis_ratio)

    T = transition_matrix_iitm(particle, lambda_um, nmax, Nr, Ntheta)
    diagnostics = build_solver_diagnostics(
        solver_requested = solver_requested,
        solver_used = "iitm",
        attempted_solvers = attempted_solvers,
        nmax = nmax,
        Nr = Nr,
        Ntheta = Ntheta,
    )
    return finalize_single_particle_result(T, lambda_um, shape_type, solver_requested, "iitm", diagnostics, angles_deg)
end

function single_particle_tm_iitm(radius_um::Float64,
                                 m::ComplexF64,
                                 wavelength_m::Float64;
                                 shape_type::String = "spheroid",
                                 r_eff::Float64 = 0.0,
                                 axis_ratio::Float64 = 1.0,
                                 Nr::Int = 64,
                                 Ntheta::Int = 96,
                                 nmax_override::Int = 0,
                                 angles_deg::Vector{Float64} = generate_adaptive_angles(),
                                 solver_requested::String = "iitm_only")
    solver = normalize_solver_choice(solver_requested)
    solver == "iitm_only" || error("current 11-figure workflow only supports iitm_only")
    return single_particle_iitm(
        radius_um,
        m,
        wavelength_m;
        shape_type = shape_type,
        r_eff = r_eff,
        axis_ratio = axis_ratio,
        Nr = Nr,
        Ntheta = Ntheta,
        nmax_override = nmax_override,
        angles_deg = angles_deg,
        solver_requested = solver,
        attempted_solvers = ["iitm"],
    )
end
