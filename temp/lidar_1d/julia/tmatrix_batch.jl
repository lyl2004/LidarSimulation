#!/usr/bin/env julia

# Temp-local T-matrix batch bridge for the 1D lidar subproject.
# It reuses the project Julia physics implementation by keeping a copied
# iitm_physics.jl next to this wrapper, so Python scripts under temp do not
# import files from src/ at runtime.

using JSON3

const HERE = @__DIR__
include(joinpath(HERE, "iitm_physics.jl"))

function read_json(path::String)
    return JSON3.read(read(path, String))
end

function as_float(obj, key::String, default::Float64)
    return Float64(get(obj, key, default))
end

function as_int(obj, key::String, default::Int)
    return Int(get(obj, key, default))
end

function as_string(obj, key::String, default::String)
    return String(get(obj, key, default))
end

function task_to_config(task)
    solver = as_string(task, "solver", "iitm_only")
    solver == "iitm_only" || error("current 11-figure workflow only supports solver=iitm_only")
    return Dict{String, Any}(
        "wavelength_m" => as_float(task, "wavelength_m", 1.55e-6),
        "m_real" => as_float(task, "m_real", 1.53),
        "m_imag" => abs(as_float(task, "m_imag", 0.004)),
        "shape_type" => as_string(task, "shape_type", "spheroid"),
        "axis_ratio" => as_float(task, "axis_ratio", 1.6),
        "size_mode" => "lognormal",
        "median_radius_um" => as_float(task, "median_radius_um", 0.6),
        "sigma_ln" => log(as_float(task, "sigma_g", 2.3)),
        "r_min_um" => as_float(task, "r_min_um", 0.01),
        "r_max_um" => as_float(task, "r_max_um", 10.0),
        "n_radii" => as_int(task, "n_radii", 17),
        "radius_quadrature" => as_string(task, "radius_quadrature", "global_gl"),
        "radius_segments" => as_int(task, "radius_segments", 1),
        "Nr" => as_int(task, "Nr", 50),
        "Ntheta" => as_int(task, "Ntheta", 80),
        "nmax_override" => as_int(task, "nmax_override", 0),
        "tmatrix_solver" => solver,
        "forward_mode" => "cone_avg",
        "forward_cone_deg" => as_float(task, "forward_cone_deg", 0.5),
    )
end

function gauss_hermite_nodes_weights(n::Int)
    n <= 0 && error("Gauss-Hermite n must be positive")
    if n == 1
        return [0.0], [sqrt(pi)]
    end
    offdiag = [sqrt(i / 2.0) for i in 1:(n - 1)]
    J = SymTridiagonal(zeros(Float64, n), offdiag)
    eig = eigen(J)
    nodes = collect(eig.values)
    weights = sqrt(pi) .* (eig.vectors[1, :] .^ 2)
    order = sortperm(nodes)
    return nodes[order], weights[order]
end

function gauss_legendre_nodes_weights(n::Int)
    n <= 0 && error("Gauss-Legendre n must be positive")
    if n == 1
        return [0.0], [2.0]
    end
    offdiag = [i / sqrt(4.0 * i^2 - 1.0) for i in 1:(n - 1)]
    J = SymTridiagonal(zeros(Float64, n), offdiag)
    eig = eigen(J)
    nodes = collect(eig.values)
    weights = 2.0 .* (eig.vectors[1, :] .^ 2)
    order = sortperm(nodes)
    return nodes[order], weights[order]
end

function simpson_weights_uniform(n::Int, a::Float64, b::Float64)
    n >= 3 || error("Simpson grid requires at least 3 nodes")
    isodd(n) || error("Simpson grid requires an odd number of nodes")
    h = (b - a) / (n - 1)
    weights = fill(2.0 * h / 3.0, n)
    weights[1] = h / 3.0
    weights[end] = h / 3.0
    for i in 2:2:(n - 1)
        weights[i] = 4.0 * h / 3.0
    end
    return weights
end

function truncated_lognormal_gl_grid(
    n_radii::Int,
    radius_segments::Int,
    r_min::Float64,
    r_max::Float64,
    median_r::Float64,
    sigma_ln::Float64,
)
    n_radii <= 0 && error("n_radii must be positive")
    radius_segments = max(1, min(radius_segments, n_radii))
    log_min = log(r_min)
    log_max = log(r_max)
    if radius_segments == 1
        segment_counts = [n_radii]
    else
        base = div(n_radii, radius_segments)
        rem = n_radii - base * radius_segments
        segment_counts = [base + (i <= rem ? 1 : 0) for i in 1:radius_segments]
    end

    r_grid = Float64[]
    weights = Float64[]
    for i in 1:radius_segments
        seg_min = log_min + (i - 1) * (log_max - log_min) / radius_segments
        seg_max = log_min + i * (log_max - log_min) / radius_segments
        nodes, gl_weights = gauss_legendre_nodes_weights(segment_counts[i])
        log_mid = 0.5 * (seg_min + seg_max)
        log_half = 0.5 * (seg_max - seg_min)
        log_r = log_mid .+ log_half .* nodes
        normal_u = exp.(-0.5 .* ((log_r .- log(median_r)) ./ sigma_ln).^2) ./
                   (sigma_ln * sqrt(2pi))
        append!(r_grid, exp.(log_r))
        append!(weights, gl_weights .* log_half .* normal_u)
    end
    order = sortperm(r_grid)
    return r_grid[order], weights[order], segment_counts
end

function truncated_lognormal_simpson_grid(
    n_radii::Int,
    r_min::Float64,
    r_max::Float64,
    median_r::Float64,
    sigma_ln::Float64,
)
    n_radii = max(3, n_radii)
    isodd(n_radii) || (n_radii += 1)
    log_min = log(r_min)
    log_max = log(r_max)
    log_r = collect(range(log_min, log_max; length = n_radii))
    base_weights = simpson_weights_uniform(n_radii, log_min, log_max)
    normal_u = exp.(-0.5 .* ((log_r .- log(median_r)) ./ sigma_ln).^2) ./
               (sigma_ln * sqrt(2pi))
    return exp.(log_r), base_weights .* normal_u
end

function compute_lidar_tmatrix_params(task)
    cfg = task_to_config(task)
    wavelength_m = Float64(cfg["wavelength_m"])
    lambda_um = wavelength_m * 1.0e6
    m_complex = complex(Float64(cfg["m_real"]), abs(Float64(cfg["m_imag"])))
    median_r = Float64(cfg["median_radius_um"])
    sigma_ln = Float64(cfg["sigma_ln"])
    r_min = Float64(cfg["r_min_um"])
    r_max = Float64(cfg["r_max_um"])
    n_radii = Int(cfg["n_radii"])
    radius_quadrature_mode = String(cfg["radius_quadrature"])
    radius_segments = max(1, Int(cfg["radius_segments"]))
    if n_radii <= 1 || sigma_ln <= 1e-6
        r_grid = [clamp(median_r, r_min, r_max)]
        weights = [1.0]
        radius_quadrature = "monodisperse representative radius"
        radius_segment_counts = [1]
    elseif radius_quadrature_mode == "log_uniform_simpson"
        r_grid, weights = truncated_lognormal_simpson_grid(
            n_radii,
            r_min,
            r_max,
            median_r,
            sigma_ln,
        )
        radius_segment_counts = [length(r_grid)]
        radius_quadrature = "Composite Simpson over uniform truncated ln(r) PDF grid"
    else
        if radius_quadrature_mode == "global_gl"
            radius_segments = 1
        end
        r_grid, weights, radius_segment_counts = truncated_lognormal_gl_grid(
            n_radii,
            radius_segments,
            r_min,
            r_max,
            median_r,
            sigma_ln,
        )
        radius_quadrature = radius_segments <= 1 ?
            "Gauss-Legendre over truncated ln(r) PDF bounds" :
            "Composite Gauss-Legendre over $(radius_segments) equal ln(r) PDF segments"
    end

    angles_deg = generate_adaptive_angles()
    nang = length(angles_deg)
    f11_sum = zeros(Float64, nang)
    f22_sum = zeros(Float64, nang)
    m12_sum = zeros(Float64, nang)
    m33_sum = zeros(Float64, nang)
    m34_sum = zeros(Float64, nang)
    sigma_ext_vals = Float64[]
    sigma_sca_vals = Float64[]
    g_sigma_sca_vals = Float64[]
    f11_vals = Vector{Vector{Float64}}()
    f22_vals = Vector{Vector{Float64}}()
    m12_vals = Vector{Vector{Float64}}()
    m33_vals = Vector{Vector{Float64}}()
    m34_vals = Vector{Vector{Float64}}()
    valid_r = Float64[]
    valid_w = Float64[]
    solver_used_per_radius = String[]
    solver_paths = String[]
    nmax_values = Int[]

    for (r, w) in zip(r_grid, weights)
        w <= 0.0 && continue
        local res
        try
            res = single_particle_tm_iitm(
                Float64(r),
                m_complex,
                wavelength_m;
                shape_type = String(cfg["shape_type"]),
                axis_ratio = Float64(cfg["axis_ratio"]),
                Nr = Int(cfg["Nr"]),
                Ntheta = Int(cfg["Ntheta"]),
                nmax_override = Int(cfg["nmax_override"]),
                angles_deg = angles_deg,
                solver_requested = String(cfg["tmatrix_solver"]),
            )
        catch e
            @warn "skip radius $(round(r,digits=4)) um: $(sprint(showerror, e))"
            continue
        end

        F = scattering_matrix(res.T_matrix, lambda_um, angles_deg)
        f11 = Float64.(real.(F[:, 1]))
        m12 = Float64.(real.(F[:, 2]))
        f22 = Float64.(real.(F[:, 3]))
        m33 = Float64.(real.(F[:, 4]))
        m34 = Float64.(real.(F[:, 5]))

        push!(valid_r, Float64(r))
        push!(valid_w, Float64(w))
        push!(sigma_ext_vals, Float64(res.sigma_ext))
        push!(sigma_sca_vals, Float64(res.sigma_sca))
        push!(g_sigma_sca_vals, Float64(res.g * res.sigma_sca))
        push!(f11_vals, f11)
        push!(f22_vals, f22)
        push!(m12_vals, m12)
        push!(m33_vals, m33)
        push!(m34_vals, m34)
        push!(solver_used_per_radius, String(res.solver_used))
        push!(solver_paths, String(get(res.diagnostics, "solver_path", res.solver_used)))
        push!(nmax_values, Int(get(res.diagnostics, "nmax", 0)))

    end

    !isempty(valid_r) || error("all T-matrix radii failed")

    # Quadrature weights already represent the configured log-radius PDF
    # integral. Do not renormalize or integrate over radius again; PDF Eq. (7)
    # and Eq. (8) use the finite radius bounds supplied by the Python driver.
    sigma_ext_eff = sum(valid_w .* sigma_ext_vals)
    sigma_sca_eff = sum(valid_w .* sigma_sca_vals)
    g_eff = sum(valid_w .* g_sigma_sca_vals) / max(sigma_sca_eff, 1e-300)

    @inbounds for i in eachindex(valid_w)
        f11_sum .+= valid_w[i] * sigma_sca_vals[i] .* f11_vals[i]
        f22_sum .+= valid_w[i] * sigma_sca_vals[i] .* f22_vals[i]
        m12_sum .+= valid_w[i] * sigma_sca_vals[i] .* m12_vals[i]
        m33_sum .+= valid_w[i] * sigma_sca_vals[i] .* m33_vals[i]
        m34_sum .+= valid_w[i] * sigma_sca_vals[i] .* m34_vals[i]
    end

    if sigma_sca_eff > 1e-40
        f11_sum ./= sigma_sca_eff
        f22_sum ./= sigma_sca_eff
        m12_sum ./= sigma_sca_eff
        m33_sum ./= sigma_sca_eff
        m34_sum ./= sigma_sca_eff
    end
    theta_r = deg2rad.(angles_deg)
    norm_val = trapz(max.(f11_sum, 0.0) .* sin.(theta_r), theta_r)
    if norm_val > 1e-20
        norm_factor = 2.0 / norm_val
        f11_sum .*= norm_factor
        f22_sum .*= norm_factor
        m12_sum .*= norm_factor
        m33_sum .*= norm_factor
        m34_sum .*= norm_factor
    end

    f11_back = f11_sum[end]
    f22_back = f22_sum[end]
    depol_back = clamp((f11_back - f22_back) / max(f11_back + f22_back, 1e-300), 0.0, 1.0)
    # PDF Eq. (8) uses Qback*pi*r^2. With F11 normalized to
    # integral(F11*sin(theta)dtheta)=2, the matching backscatter cross-section
    # convention is sigma_sca*F11(180), not the differential-per-steradian
    # value sigma_sca*F11(180)/(4*pi).
    sigma_back_ref = sigma_sca_eff * f11_back
    unique_solvers = unique(solver_used_per_radius)
    solver_used = length(unique_solvers) == 1 ? unique_solvers[1] : "mixed"
    path_counts = Dict{String, Int}()
    for path in solver_paths
        path_counts[path] = get(path_counts, path, 0) + 1
    end
    solver_path_summary = isempty(path_counts) ? "" :
        join(["$(k) × $(v)" for (k, v) in sort(collect(path_counts); by = first)], ", ")

    return Dict{String, Any}(
        "sigma_ext" => sigma_ext_eff,
        "sigma_sca" => sigma_sca_eff,
        "sigma_back_ref" => sigma_back_ref,
        "depol_back_lidar" => depol_back,
        "g" => g_eff,
        "omega0" => sigma_ext_eff > 1e-40 ? clamp(sigma_sca_eff / sigma_ext_eff, 0.0, 1.0) : 0.0,
        "phase_m11_back" => f11_back,
        "phase_m22_back" => f22_back,
        "angles_deg" => angles_deg,
        "M11" => f11_sum,
        "M12" => m12_sum,
        "M33" => m33_sum,
        "M34" => m34_sum,
        "radius_quadrature" => radius_quadrature,
        "radius_segments" => radius_segments,
        "radius_segment_counts" => radius_segment_counts,
        "n_radius_nodes_requested" => n_radii,
        "n_radius_nodes_used" => length(r_grid),
        "r_min_um" => r_min,
        "r_max_um" => r_max,
        "quadrature_weight_sum" => sum(weights),
        "valid_quadrature_weight_sum" => sum(valid_w),
        "solver_used" => solver_used,
        "solver_path_summary" => solver_path_summary,
        "iitm_count" => count(==("iitm"), solver_used_per_radius),
        "nmax_min" => minimum(nmax_values),
        "nmax_max" => maximum(nmax_values),
        "iitm_nr" => Int(cfg["Nr"]),
        "iitm_ntheta" => Int(cfg["Ntheta"]),
    )
end

function solve_task(task)
    key = as_string(task, "key", "unnamed")
    n0_m3 = as_float(task, "n0_cm3", 0.0) * 1.0e6
    t0 = time()
    try
        sc = compute_lidar_tmatrix_params(task)
        sigma_ext = Float64(sc["sigma_ext"])
        sigma_back = Float64(sc["sigma_back_ref"])
        return Dict{String, Any}(
            "key" => key,
            "ok" => true,
            "solver_requested" => as_string(task, "solver", "iitm_only"),
            "solver_used" => String(sc["solver_used"]),
            "radius_quadrature" => String(sc["radius_quadrature"]),
            "radius_segments" => Int(sc["radius_segments"]),
            "radius_segment_counts" => sc["radius_segment_counts"],
            "n_radius_nodes_requested" => Int(sc["n_radius_nodes_requested"]),
            "n_radius_nodes_used" => Int(sc["n_radius_nodes_used"]),
            "r_min_um" => Float64(sc["r_min_um"]),
            "r_max_um" => Float64(sc["r_max_um"]),
            "quadrature_weight_sum" => Float64(sc["quadrature_weight_sum"]),
            "valid_quadrature_weight_sum" => Float64(sc["valid_quadrature_weight_sum"]),
            "solver_path_summary" => String(get(sc, "solver_path_summary", "")),
            "sigma_ext_m2" => sigma_ext,
            "sigma_sca_m2" => Float64(sc["sigma_sca"]),
            "sigma_back_ref_m2_sr" => sigma_back,
            "sigma_back_convention" => "PDF Eq. (8) analog: sigma_sca*F11(180)",
            "alpha_particle_m_inv" => n0_m3 * sigma_ext,
            "beta_particle_m_inv_sr" => n0_m3 * sigma_back,
            "depol_back" => Float64(sc["depol_back_lidar"]),
            "depol_definition" => "lidar_linear_delta=(F11-F22)/(F11+F22) at 180 deg",
            "g" => Float64(sc["g"]),
            "omega0" => Float64(sc["omega0"]),
            "phase_m11_back" => Float64(sc["phase_m11_back"]),
            "phase_m22_back" => Float64(sc["phase_m22_back"]),
            "angles_deg" => sc["angles_deg"],
            "M11" => sc["M11"],
            "M12" => sc["M12"],
            "M33" => sc["M33"],
            "M34" => sc["M34"],
            "nmax_min" => Int(sc["nmax_min"]),
            "nmax_max" => Int(sc["nmax_max"]),
            "iitm_count" => Int(sc["iitm_count"]),
            "iitm_nr" => Int(sc["iitm_nr"]),
            "iitm_ntheta" => Int(sc["iitm_ntheta"]),
            "elapsed_s" => time() - t0,
        )
    catch err
        return Dict{String, Any}(
            "key" => key,
            "ok" => false,
            "solver_requested" => as_string(task, "solver", "iitm_only"),
            "error" => sprint(showerror, err),
            "elapsed_s" => time() - t0,
        )
    end
end

function main()
    length(ARGS) >= 2 || error("usage: julia --project=. tmatrix_batch.jl request.json response.json")
    req = read_json(ARGS[1])
    tasks = get(req, "tasks", [])
    results = Any[]
    for task in tasks
        push!(results, solve_task(task))
    end
    output = Dict{String, Any}(
        "ok" => all(Bool(get(r, "ok", false)) for r in results),
        "julia_version" => string(VERSION),
        "thread_count" => Threads.nthreads(),
        "results" => results,
    )
    open(ARGS[2], "w") do io
        JSON3.write(io, output)
    end
end

main()
