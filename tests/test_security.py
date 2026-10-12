import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from core.security_manager import SecurityManager, RiskLevel, SecurityError


def test_safe_action_auto_approves_without_callback():
    sm = SecurityManager()
    assert sm.request_confirmation("open calculator", RiskLevel.SAFE) is True


def test_high_risk_denied_without_callback():
    sm = SecurityManager()
    assert sm.request_confirmation("delete important_file.txt", RiskLevel.HIGH_RISK) is False


def test_high_risk_approved_with_callback():
    sm = SecurityManager()
    sm.set_confirm_callback(lambda msg: True)
    assert sm.request_confirmation("delete file.txt", RiskLevel.HIGH_RISK) is True


def test_high_risk_declined_with_callback():
    sm = SecurityManager()
    sm.set_confirm_callback(lambda msg: False)
    assert sm.request_confirmation("format drive", RiskLevel.CRITICAL) is False


def test_blocked_pattern_raises():
    sm = SecurityManager()
    try:
        sm.check_and_confirm("run_shell", RiskLevel.HIGH_RISK, details="format c:")
        assert False, "expected SecurityError"
    except SecurityError:
        pass


def test_protected_path_blocked():
    sm = SecurityManager()
    assert sm.validate_path("C:\\Windows\\System32\\drivers") is False


def test_normal_path_allowed():
    sm = SecurityManager()
    assert sm.validate_path(str(Path.home())) is True


if __name__ == "__main__":
    import inspect
    fails = 0
    for name, fn in list(globals().items()):
        if name.startswith("test_") and inspect.isfunction(fn):
            try:
                fn()
                print(f"PASS {name}")
            except AssertionError as e:
                fails += 1
                print(f"FAIL {name}: {e}")
    sys.exit(1 if fails else 0)
