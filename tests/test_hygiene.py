"""Offline checks for test storage and backtest provenance."""
import json
import os
from pathlib import Path
import shutil
import subprocess
import sys
import tempfile
from datetime import datetime, timedelta
from unittest.mock import patch

os.environ.setdefault("OKXBOT_DATA_DIR", tempfile.mkdtemp())
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from okxbot import config
from backtests.provenance import provenance


def t_data_dir_is_isolated():
    assert Path(config.DATA_DIR).resolve() != Path(config.ROOT_DIR, "data").resolve()
    assert config.DATA_DIR == os.environ["OKXBOT_DATA_DIR"]


def t_provenance_filters_secrets_and_non_scalars():
    values = {"API_KEY": "key-sentinel", "SECRET_TOKEN": "secret-sentinel",
              "PASSPHRASE": "pass-sentinel", "TEST_LIST": [1, 2],
              "lowercase": "excluded"}
    with patch.dict(vars(config), values), patch(
        "backtests.provenance.subprocess.check_output",
        side_effect=["abc123\n", " M okxbot/config.py\n"],
    ) as git:
        result = provenance()
    assert result["git_commit"] == "abc123"
    assert result["dirty"] is True
    assert datetime.fromisoformat(result["timestamp_utc"]).utcoffset() == timedelta(0)
    assert result["config"]["DEMO"] is True
    assert result["config"]["RISK_PER_TRADE"] == config.RISK_PER_TRADE
    assert not set(values).intersection(result["config"])
    assert all(not any(token in name for token in ("KEY", "SECRET", "PASS"))
               for name in result["config"])
    encoded = json.dumps(result)
    assert all(s not in encoded for s in ("key-sentinel", "secret-sentinel", "pass-sentinel"))
    assert all(call.kwargs["cwd"] == config.ROOT_DIR for call in git.call_args_list)


def t_provenance_clean_and_git_unavailable():
    with patch("backtests.provenance.subprocess.check_output", side_effect=["abc123", ""]):
        assert provenance()["dirty"] is False
    for error in (FileNotFoundError(), subprocess.CalledProcessError(128, "git")):
        with patch("backtests.provenance.subprocess.check_output", side_effect=error):
            result = provenance()
        assert result["git_commit"] == "unknown"
        assert result["dirty"] is None
        json.dumps(result)


def t_runner_isolates_storage_and_reports_failures():
    with tempfile.TemporaryDirectory() as directory:
        tests = Path(directory, "tests")
        tests.mkdir()
        shutil.copyfile(Path(__file__).with_name("run_all.py"), tests / "run_all.py")
        (tests / "test_a.py").write_text(
            "import os\n"
            "assert os.environ['OKXBOT_DATA_DIR'] != 'must-be-overridden'\n"
            "assert os.path.isdir(os.environ['OKXBOT_DATA_DIR'])\n"
        )
        (tests / "test_b.py").write_text("raise AssertionError('expected failure')\n")
        (tests / "test_c.py").write_text("print('last module ran')\n")
        env = dict(os.environ, OKXBOT_DATA_DIR="must-be-overridden")
        result = subprocess.run([sys.executable, str(tests / "run_all.py")],
                                env=env, capture_output=True, text=True)
        assert result.returncode == 1
        assert "2/3 test modules passed" in result.stdout
        assert "FAILED: test_b.py" in result.stdout
        assert "last module ran" in result.stdout
        (tests / "test_b.py").unlink()
        result = subprocess.run([sys.executable, str(tests / "run_all.py")],
                                env=env, capture_output=True, text=True)
        assert result.returncode == 0, result.stderr
        assert "2/2 test modules passed" in result.stdout


if __name__ == "__main__":
    for name, func in list(globals().items()):
        if name.startswith("t_"):
            func()
            print("PASS", name)
