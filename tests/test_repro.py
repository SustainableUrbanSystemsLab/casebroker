"""`casebroker triage` and `repro`'s case copy, on the logs of the case that motivated them.

The excerpts are v2-00697fb4542aa4c6's (2026-09-22), trimmed: the reproduction
whose snappy died mid-snap and whose mesh held 69 regions, and the fixed run of
the same site, which is the control -- every signature must stay silent there.
"""

import json

from casebroker import cli, repro

FAILED_CHECKMESH = """\
Checking topology...
    Boundary definition OK.
   *Number of regions: 69
    The mesh has multiple regions which are not connected by any face.
Checking geometry...
    Mesh non-orthogonality Max: 47.581744 average: 11.741081
    Max skewness = 1.2812723 OK.

Mesh OK.

End
"""

FIXED_CHECKMESH = """\
Checking topology...
    Number of regions: 1 (OK).
Checking geometry...
    Mesh non-orthogonality Max: 64.999331 average: 12.46318
 ***Max skewness = 13.992747, 111 highly skew faces detected which may impair the quality of the results

Failed 1 mesh checks.

End
"""

SNAPPY_DIED = """\
Snapping to features in 10 iterations ...
Smoothing displacement ...
Exec   : reconstructPar -constant -latestTime -noFields
Time = constant

End
"""

SNAPPY_FINISHED = """\
Finished meshing with 1 illegal faces (concave, zero area or negative cell pyramid volume)
Finished meshing in = 1516.242 s.
End
"""

POTENTIALFOAM_ZERO_PIVOT = """\
Exec   : potentialFoam -initialiseUBCs -parallel
Calculating potential flow
[1] Generating stack trace...
\tZN4Foam6sigFpe13sigFpeHandlerEi [0x7ffff6ffc774+0x34]
\tZN4Foam17DICPreconditioner15calcReciprocalDERNS_5FieldIdEERKNS_9lduMatrixE [0x7ffff6e99a68+0xc8]
job aborted:
potentialFoam ended prematurely and may have crashed. exit code 3
"""

# Five rungs appended to one log (the step list tees with -a), each dead in iteration 1.
FOAMRUN_FIVE_RUNGS = "".join(f"""\
Exec   : foamRun -solver incompressibleFluid -parallel
Starting time loop
Time = 1s
DILUPBiCGStab:  Solving for Ux, Initial residual = 1, Final residual = 0.017, No Iterations 1
[1] Generating stack trace...
\tZN4Foam17DICPreconditioner15calcReciprocalDERNS_5FieldIdEERKNS_9lduMatrixE [0x7ffff6e99a68+0xc8]
job aborted:
foamRun ended prematurely and may have crashed. exit code 3
""" for _ in range(5))

HEALTHY_WARMUP = "Exec   : foamRun -solver incompressibleFluid -parallel\n" + "".join(
    f"Time = {i}s\ntime step continuity errors : sum local = 1.4e-08\n" for i in range(1, 18))


def _study(tmp_path, *, checkmesh, snappy, pf, runs):
    study = tmp_path / "v2-00697fb4542aa4c6"
    (study / "mesh").mkdir(parents=True)
    (study / "mesh" / "06_checkMesh.txt").write_text(checkmesh)
    (study / "mesh" / "03_snappyHexMesh.txt").write_text(snappy)
    case = study / "case_000"
    case.mkdir()
    (case / "run_00b_potentialFoam.txt").write_text(pf)
    for name, text in runs.items():
        (case / name).write_text(text)
    return study


def test_triage_names_all_three_causes_of_the_failed_reproduction(tmp_path):
    study = _study(tmp_path, checkmesh=FAILED_CHECKMESH, snappy=SNAPPY_DIED,
                   pf=POTENTIALFOAM_ZERO_PIVOT, runs={"run_01_foamRun.txt": FOAMRUN_FIVE_RUNGS})
    found = repro.signatures(repro.triage(study))
    text = "\n".join(found)

    assert any("never printed 'Finished meshing'" in f for f in found), text
    assert any("69 disconnected mesh regions" in f for f in found), text
    assert any("ZERO PIVOT" in f and "run_00b_potentialFoam.txt" in f for f in found), text
    # Counted after the last banner: five dead rungs are not "five iterations".
    assert any("died in iteration 1" in f for f in found), text


def test_triage_is_silent_on_the_fixed_run_of_the_same_site(tmp_path):
    study = _study(tmp_path, checkmesh=FIXED_CHECKMESH, snappy=SNAPPY_FINISHED,
                   pf="Exec   : potentialFoam -initialiseUBCs -parallel\nCalculating potential flow\nEnd\n",
                   runs={"run_01a_warmup.txt": HEALTHY_WARMUP})
    findings = repro.triage(study)

    assert repro.signatures(findings) == [], findings
    # The mesh numbers are still reported, as context rather than a signature.
    assert any(f.startswith("info:") and "13.992747" in f for f in findings)


def test_triage_command_exit_code_follows_the_signatures(tmp_path, capsys):
    failed = _study(tmp_path / "a", checkmesh=FAILED_CHECKMESH, snappy=SNAPPY_FINISHED,
                    pf="", runs={})
    fixed = _study(tmp_path / "b", checkmesh=FIXED_CHECKMESH, snappy=SNAPPY_FINISHED,
                   pf="", runs={})
    assert cli.main(["triage", str(failed)]) == 1
    assert "69 disconnected" in capsys.readouterr().out
    assert cli.main(["triage", str(fixed)]) == 0
    assert "no known signature" in capsys.readouterr().out


def test_case_input_recreates_the_production_row_with_one_attempt():
    spec = {"lat": 10.16062, "lon": 76.19952, "lcz": "LCZ9", "recipe": "cyl-1008/of12-v4",
            "sampler_version": "sampler-v3"}
    row = {"case_id": "v2-00697fb4542aa4c6", "recipe": "cyl-1008/of12-v4",
           "city_cluster": "c11_83", "lcz": "LCZ9", "spec": json.dumps(spec)}  # the API sends it as a string

    [body] = repro.case_input(row)
    assert body["spec"] == spec
    assert (body["lat"], body["lon"]) == (10.16062, 76.19952)
    assert body["recipe"] == "cyl-1008/of12-v4" and body["city_cluster"] == "c11_83"
    assert body["max_attempts"] == 1


def test_triage_names_a_cold_start_blowup_and_not_a_healthy_start(tmp_path):
    """v2-00283bf3eca50684 (2026-09-22): continuity 2.9e10 -> 1.5e15 -> 4.6e136 in four
    iterations. The control is the fixed Kerala warm-up at 1.4e-8."""
    blowup = """\
Exec   : foamRun -solver incompressibleFluid -parallel
Time = 2s
time step continuity errors : sum local = 2.9263262e+10, global = 3.03e+08
Time = 3s
time step continuity errors : sum local = 1.4713554e+15, global = 4.74e+12
Time = 4s
time step continuity errors : sum local = 4.5712051e+136, global = -1.67e+126
[11] Generating stack trace...
job aborted:
"""
    study = _study(tmp_path / "a", checkmesh=FIXED_CHECKMESH, snappy=SNAPPY_FINISHED, pf="",
                   runs={"run_01_foamRun.txt": blowup})
    found = repro.signatures(repro.triage(study))
    assert any("DIVERGED" in f and "4.6e+136" in f for f in found), found
    assert not any("ZERO PIVOT" in f for f in found), found

    healthy = _study(tmp_path / "b", checkmesh=FIXED_CHECKMESH, snappy=SNAPPY_FINISHED, pf="",
                     runs={"run_01a_warmup.txt": HEALTHY_WARMUP})
    assert repro.signatures(repro.triage(healthy)) == []
