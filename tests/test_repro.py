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


# ICE job 6093036 (2026-10-07), trimmed: the container ran as uid 0 without the two
# OMPI_ALLOW_RUN_AS_ROOT variables, and three healthy cases were charged "meshing exited 1 at
# 03_snappyHexMesh" in two minutes -- v2-004420ca019825d7's first attempt among them.
SNAPPY_AS_ROOT = """\
Number of processor faces = 22709
Max number of cells = 9222 (1.4219269% above average 9092.7083)

End

--------------------------------------------------------------------------
mpirun has detected an attempt to run as root.

Running as root is *strongly* discouraged as any mistake (e.g., in
defining TMPDIR) or bug can result in catastrophic damage to the OS
file system, leaving your system in an unusable state.
--------------------------------------------------------------------------
"""


def test_triage_names_mpirun_refusing_root_instead_of_a_snappy_that_died(tmp_path):
    study = _study(tmp_path, checkmesh="", snappy=SNAPPY_AS_ROOT, pf="", runs={})
    found = repro.signatures(repro.triage(study))

    assert any("mpirun refused to start as root (03_snappyHexMesh.txt)" in f
               and "OMPI_ALLOW_RUN_AS_ROOT" in f for f in found), found
    # snappy never started, so "never finished" would send the reader to the mesh.
    assert not any("never printed 'Finished meshing'" in f for f in found), found


def test_triage_names_mpirun_refusing_root_in_a_direction(tmp_path):
    study = _study(tmp_path, checkmesh=FIXED_CHECKMESH, snappy=SNAPPY_FINISHED,
                   pf="Exec   : potentialFoam -initialiseUBCs -parallel\n" + SNAPPY_AS_ROOT, runs={})
    found = repro.signatures(repro.triage(study))
    assert any(f.startswith("case_000: mpirun refused") for f in found), found


# cod-pkast-7865's console, 2026-10-09 (dumped from the window: "error" untagged, wrapped), and
# what a build from Eddy3D 1f0f6b4b on writes to a redirected log.
CONSOLE_STALL = """\
info  case_214 of v2-004420ca019825d7 is at the broker (296.6 MB)
error v2-004420ca019825d7: the heartbeat has not moved for 20 min -- something in this node is stuck where it cannot report from. The lease has
      lapsed and another worker may be given the case, so it is abandoned here rather than solved for nobody.
warn  lease lost on v2-004420ca019825d7; abandoning it (another worker may own it now)
"""

LOG_STALL_WITH_REPORT = """\
[error] c1: the heartbeat has not moved for 20 min -- something in this node is stuck where it cannot report from. The lease has lapsed and another worker may be given the case, so it is abandoned here rather than solved for nobody.
      thread pool: 512 threads, 3 work items waiting; process threads: 540; console: a redraw has held the output gate for 1203 s (the console is not taking output: a selection in the window, a dragged scrollbar?)
[warn] lease lost on c1; abandoning it (another worker may own it now)
"""


def test_triage_names_a_lease_the_node_gave_up_itself(tmp_path):
    study = _study(tmp_path, checkmesh=FIXED_CHECKMESH, snappy=SNAPPY_FINISHED, pf="",
                   runs={"run_01a_warmup.txt": HEALTHY_WARMUP})
    console = tmp_path / "console.txt"
    console.write_text(CONSOLE_STALL)
    log = tmp_path / "node.log"
    log.write_text(LOG_STALL_WITH_REPORT)

    [old] = repro.signatures(repro.triage(study, node_log=console))
    assert old.startswith("node: v2-004420ca019825d7 was given up by its own node") and "20 min" in old
    assert "older than 1f0f6b4b" in old and "529fa324" in old and "Reopen" in old

    [new] = repro.signatures(repro.triage(study, node_log=log))
    assert "stall report: thread pool: 512 threads, 3 work items waiting" in new
    assert "held the output gate for 1203 s" in new

    # The control: a node log with no stall says nothing, and the study alone is still clean.
    quiet = tmp_path / "quiet.log"
    quiet.write_text("[info] leased c1 (attempt 1)\n[warn] heartbeat failed: HTTP 522\n")
    assert repro.signatures(repro.triage(study, node_log=quiet)) == []


def test_triage_command_reads_the_node_log(tmp_path, capsys):
    study = _study(tmp_path, checkmesh=FIXED_CHECKMESH, snappy=SNAPPY_FINISHED, pf="", runs={})
    log = tmp_path / "node.log"
    log.write_text(LOG_STALL_WITH_REPORT)
    assert cli.main(["triage", str(study), "--node-log", str(log)]) == 1
    assert "c1 was given up by its own node" in capsys.readouterr().out
