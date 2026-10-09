"""The PACE job scripts, run here against stand-ins for SLURM, ssh and E3D.

A real PACE run needs the VPN, Duo and an allocation, so what can go wrong in the scripts
themselves -- an option an older E3D refuses, a setting that never reaches the job, a chain that
does not stop -- is checked here: each script runs under bash with sacct, sbatch, squeue, timeout,
scp, ssh and E3D replaced by small scripts that record what they were given. The whole path is
walked once, as it runs for real: scripts/pace_workers.sh copies the job files and runs
slurm/submit_workers.sh "on the login node", which submits through "sbatch", whose recorded job is
then run as SLURM would run it, down to the E3D command line and the environment it gets.
"""
from __future__ import annotations

import os
import pathlib
import shlex
import shutil
import subprocess

import pytest

ROOT = pathlib.Path(__file__).resolve().parents[1]
BASH = shutil.which("bash")
pytestmark = pytest.mark.skipif(BASH is None, reason="bash is not installed")

STUBS = {
    # E3D: one line per call -- its arguments, then what it was given for the hand-off; and, on a
    # line of its own, the file that ran (the job's copy or the installed one).
    "E3D": """#!/bin/bash
echo "E3D $* | E3D_CHUNK_HOURS=${E3D_CHUNK_HOURS-<unset>}" >> "$CALLS"
echo "ran $0" >> "$CALLS"
case "$1" in
  node-release) [ -n "${SYNC_SAYS:-}" ] && echo "$SYNC_SAYS"; exit "${SYNC_EXIT:-0}" ;;
esac
exit 0
""",
    "timeout": """#!/bin/bash
echo "timeout $1" >> "$CALLS"; shift; exec "$@"
""",
    "sacct": """#!/bin/bash
echo "${SACCT_SAYS:-}"
""",
    "nproc": "#!/bin/bash\necho 24\n",
    # sbatch: the arguments of each submission, one per line, then a blank line; prints a job id
    # the way a federated cluster does ("id;cluster"), which submit_workers.sh has to cut.
    "sbatch": """#!/bin/bash
n=$(( $(cat "$SBATCH_N" 2>/dev/null || echo 1000) + 1 )); echo $n > "$SBATCH_N"
printf '%s\\n' "$@" >> "$SUBMITTED"; echo >> "$SUBMITTED"
echo "$n;fake"
""",
    "squeue": "#!/bin/bash\nexit 0\n",
    # scp copies onto the fake cluster home; ssh runs the remote command there.
    "scp": """#!/bin/bash
echo "scp $*" >> "$CALLS"
args=(); while [ $# -gt 0 ]; do case $1 in -q) shift ;; -o) shift 2 ;; *) args+=("$1"); shift ;; esac; done
dest=${args[${#args[@]}-1]}; mkdir -p "$HOME/${dest#*:}"
for f in "${args[@]:0:${#args[@]}-1}"; do cp "$f" "$HOME/${dest#*:}"; done
""",
    "ssh": """#!/bin/bash
echo "ssh $*" >> "$CALLS"
while [ $# -gt 1 ]; do shift; done
exec bash -c "$1"
""",
}


@pytest.fixture()
def cluster(tmp_path):
    """A fake PACE account: a home with E3D installed and paired, and stand-ins on PATH."""
    home = tmp_path / "home"
    bin_ = home / "windcomfort" / "bin"
    bin_.mkdir(parents=True)
    stubs = tmp_path / "stubs"
    stubs.mkdir()
    for name, text in STUBS.items():
        target = bin_ / name if name == "E3D" else stubs / name
        target.write_text(text)
        target.chmod(0o755)
    shim = bin_ / "podman-pace.sh"
    shim.write_text("#!/bin/bash\n")
    shim.chmod(0o755)
    cred = home / ".local" / "share" / "Eddy3D" / "node" / "credential.json"
    cred.parent.mkdir(parents=True)
    cred.write_text('{"name": "ice"}')
    (tmp_path / "scratch").mkdir()
    env = {
        "HOME": str(home), "USER": "tester", "PATH": f"{stubs}:/usr/bin:/bin",
        "TMPDIR": str(tmp_path / "scratch"), "E3D_DONE": str(tmp_path / "done"),
        "CALLS": str(tmp_path / "calls.log"), "SUBMITTED": str(tmp_path / "submitted.log"),
        "SBATCH_N": str(tmp_path / "sbatch.n"), "SLURM_JOB_ID": "4242", "SLURM_NTASKS": "24",
    }
    return {"home": home, "bin": bin_, "env": env, "tmp": tmp_path}


def _run(cluster, script, *args, **extra):
    env = {**cluster["env"], **{k: str(v) for k, v in extra.items()}}
    return subprocess.run([BASH, str(script), *args], env=env, cwd=cluster["tmp"],
                          capture_output=True, text=True, timeout=60)


def _calls(cluster) -> list[str]:
    log = cluster["tmp"] / "calls.log"
    return log.read_text().splitlines() if log.exists() else []


def _e3d(cluster) -> list[str]:
    return [c for c in _calls(cluster) if c.startswith("E3D ")]


def _ran(cluster) -> list[str]:
    return [c[len("ran "):] for c in _calls(cluster) if c.startswith("ran ")]


def _submissions(cluster) -> list[list[str]]:
    log = cluster["tmp"] / "submitted.log"
    return [block.split("\n") for block in log.read_text().strip("\n").split("\n\n")] if log.exists() else []


JOBS = [("ice_e3d_node.sbatch", "ICE"), ("phoenix_e3d_node.sbatch", "Phoenix")]


# -- the job -----------------------------------------------------------------------------------

@pytest.mark.parametrize("job,name", JOBS)
def test_a_job_takes_the_target_build_then_runs_the_node_handing_off_after_7_h(cluster, job, name):
    r = _run(cluster, ROOT / "slurm" / job)
    assert r.returncode == 0, r.stdout + r.stderr
    exe = cluster["bin"] / "E3D"
    sync, run = _e3d(cluster)
    assert sync == f"E3D node-release sync --exe {exe} | E3D_CHUNK_HOURS=7"
    assert "timeout 900" in _calls(cluster), "a download that hangs must not eat the shift"
    assert run.startswith(f"E3D run-sim-node --engine docker --cpus 24 --cluster {name} ")
    assert run.endswith("--drain --max-idle-polls 10 | E3D_CHUNK_HOURS=7")
    # Never the option: an E3D from before --chunk-hours refuses the whole command line.
    assert "--chunk-hours" not in run
    assert "hands a case on after 7 h" in r.stdout
    # sync replaces the installed E3D; the node runs from this job's own copy, which nothing
    # replaces while the job runs (run-sim-node starts every step of a case by its own path).
    synced_by, node = _ran(cluster)
    assert synced_by == str(exe)
    copy = pathlib.Path(node)
    assert copy.name == "E3D" and copy.parent.name.startswith("e3d-bin.")
    assert copy.parent.parent == pathlib.Path(cluster["env"]["TMPDIR"]), "node-local scratch, wiped with the job"


@pytest.mark.parametrize("job,name", JOBS)
def test_a_job_that_cannot_copy_e3d_runs_the_installed_one_and_says_so(cluster, job, name):
    r = _run(cluster, ROOT / "slurm" / job, TMPDIR=str(cluster["tmp"] / "no-such-dir"))
    assert r.returncode == 0, r.stdout + r.stderr
    assert "could not copy E3D to node-local scratch" in r.stdout
    assert _ran(cluster)[-1] == str(cluster["bin"] / "E3D")


@pytest.mark.parametrize("job,name", JOBS)
@pytest.mark.parametrize("hours,seen", [("6.5", "6.5"), ("0", "<unset>"), ("0.0", "<unset>"), ("", "7")])
def test_chunk_hours_reaches_e3d_as_its_variable(cluster, job, name, hours, seen):
    # E3D refuses E3D_CHUNK_HOURS=0 ("more than 0") and stops: 0 has to mean "not set at all",
    # also over a value someone exported by hand.
    r = _run(cluster, ROOT / "slurm" / job, CHUNK_HOURS=hours, E3D_CHUNK_HOURS="3")
    assert r.returncode == 0, r.stdout + r.stderr
    assert _e3d(cluster)[-1].endswith(f"| E3D_CHUNK_HOURS={seen}")
    if seen == "<unset>":
        assert "never hands a case on" in r.stdout


@pytest.mark.parametrize("job,name", JOBS)
def test_a_chunk_that_is_not_a_number_stops_the_job_before_e3d(cluster, job, name):
    r = _run(cluster, ROOT / "slurm" / job, CHUNK_HOURS="7h")
    assert r.returncode == 2
    assert "CHUNK_HOURS=7h is not a number of hours" in r.stdout
    assert _e3d(cluster) == []


@pytest.mark.parametrize("job,name", JOBS)
def test_a_build_that_cannot_sync_still_runs(cluster, job, name):
    """The first job after this lands still runs the E3D installed by hand, which has no sync."""
    r = _run(cluster, ROOT / "slurm" / job, SYNC_EXIT=2, SYNC_SAYS="unknown command: node-release sync")
    assert r.returncode == 0, r.stdout + r.stderr
    assert "E3D was not updated" in r.stdout
    assert _e3d(cluster)[-1].startswith("E3D run-sim-node ")


@pytest.mark.parametrize("job,name", JOBS)
def test_a_chain_after_a_short_job_stops_before_downloading_anything(cluster, job, name):
    r = _run(cluster, ROOT / "slurm" / job, CHAIN_PREV="4100", SACCT_SAYS="FAILED|12")
    assert r.returncode == 0
    assert "chain stopped: job 4100 (FAILED) ran 12s" in r.stdout
    assert _e3d(cluster) == []
    r = _run(cluster, ROOT / "slurm" / job, CHAIN_PREV="4100", SACCT_SAYS="TIMEOUT|28800", MAX_CASES=3)
    assert r.returncode == 0, r.stdout + r.stderr
    assert " --max-cases 3 " in _e3d(cluster)[-1]


# -- submitting --------------------------------------------------------------------------------

def test_a_dry_run_shows_the_chain_and_the_hand_off(cluster):
    r = _run(cluster, ROOT / "slurm" / "submit_workers.sh", "chain", "3", "--chunk-hours", "0", "--dry-run")
    assert r.returncode == 0, r.stderr
    lines = [x for x in r.stdout.splitlines() if x.startswith("sbatch ")]
    assert len(lines) == 3
    assert "--export=ALL,CHAIN_PREV=,CHUNK_HOURS=0 " in lines[0] and "--dependency" not in lines[0]
    assert "--export=ALL,CHAIN_PREV=dry-1-1,CHUNK_HOURS=0 --dependency=afterany:dry-1-1 " in lines[1]
    r = _run(cluster, ROOT / "slurm" / "submit_workers.sh", "parallel", "2", "--dry-run")
    assert "CHUNK_HOURS" not in r.stdout, "the job's own default, unless asked"


def test_a_chunk_that_is_not_a_number_is_refused_before_submitting(cluster):
    r = _run(cluster, ROOT / "slurm" / "submit_workers.sh", "chain", "3", "--chunk-hours", "seven")
    assert r.returncode == 1 and "--chunk-hours wants hours" in r.stderr
    assert _submissions(cluster) == []


def test_an_unpaired_account_is_one_sentence_not_a_chain_of_failed_jobs(cluster):
    (cluster["home"] / ".local" / "share" / "Eddy3D" / "node" / "credential.json").unlink()
    r = _run(cluster, ROOT / "slurm" / "submit_workers.sh", "chain", "2")
    assert r.returncode == 1 and "this account is not paired" in r.stderr and "--name ice" in r.stderr
    assert _submissions(cluster) == []


def test_from_this_machine_to_the_e3d_command_line(cluster):
    """pace_workers.sh -> scp + ssh -> submit_workers.sh -> sbatch -> the job -> E3D."""
    r = _run(cluster, ROOT / "scripts" / "pace_workers.sh", "ice", "chain", "2", "--chunk-hours", "6.5")
    assert r.returncode == 0, r.stdout + r.stderr
    copied = sorted(p.name for p in cluster["bin"].iterdir())
    assert {"submit_workers.sh", "ice_e3d_node.sbatch", "podman-pace.sh"} <= set(copied)
    assert any(c.startswith("ssh -o ControlPath=/tmp/ice.sock pkastner3@login-ice.pace.gatech.edu ")
               for c in _calls(cluster))
    first, second = _submissions(cluster)
    assert first[:2] == ["--parsable", "--export=ALL,CHAIN_PREV=,CHUNK_HOURS=6.5"]
    # "1001;fake" from a federated cluster: the next job waits on 1001, not on "1001;fake".
    assert second[1:3] == ["--export=ALL,CHAIN_PREV=1001,CHUNK_HOURS=6.5", "--dependency=afterany:1001"]
    assert "lane 1 (2 jobs): 1001 -> 1002" in r.stdout
    assert (cluster["home"] / "windcomfort" / "logs").is_dir(), "where #SBATCH -o writes"

    # Now run the second job as SLURM would: its script, with what --export gave it, after the first ended.
    script = pathlib.Path(second[-1])
    assert script == cluster["bin"] / "ice_e3d_node.sbatch"
    exported = dict(kv.split("=", 1) for kv in second[1][len("--export=ALL,"):].split(","))
    job = _run(cluster, script, SACCT_SAYS="TIMEOUT|28800", SLURM_JOB_ID="1002", **exported)
    assert job.returncode == 0, job.stdout + job.stderr
    sync, run = _e3d(cluster)
    assert sync.startswith("E3D node-release sync --exe ")
    assert run.endswith("| E3D_CHUNK_HOURS=6.5") and "--cluster ICE" in run
    assert shlex.split(run.split(" | ")[0])[1] == "run-sim-node"
