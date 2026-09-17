using JSON3

include(joinpath(@__DIR__, "..", "src", "julia", "iitm_physics.jl"))

function main()
    wavelength_m = parse(Float64, ARGS[1])
    m = complex(parse(Float64, ARGS[2]), parse(Float64, ARGS[3]))
    radii_um = parse.(Float64, split(ARGS[4], ","))
    angles_deg = parse.(Float64, split(ARGS[5], ","))
    nr = parse(Int, ARGS[6])
    ntheta = parse(Int, ARGS[7])

    rows = Any[]
    for radius_um in radii_um
        result = single_particle_iitm(
            radius_um,
            m,
            wavelength_m;
            shape_type = "sphere",
            axis_ratio = 1.0,
            Nr = nr,
            Ntheta = ntheta,
            angles_deg = angles_deg,
        )
        push!(rows, Dict(
            "radius_um" => radius_um,
            "sigma_ext" => result.sigma_ext,
            "sigma_sca" => result.sigma_sca,
            "g" => result.g,
            "M11" => result.M11,
            "M12" => result.M12,
            "M22" => result.M22,
            "M33" => result.M33,
            "M34" => result.M34,
        ))
    end
    println("THEORY_PROBE_JSON=" * JSON3.write(rows))
end

main()
