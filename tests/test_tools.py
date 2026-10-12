import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from core.tool_registry import registry


def test_get_system_info_returns_dict():
    result = registry.execute("get_system_info")
    assert result.success is True
    assert isinstance(result.output, dict)
    assert "os" in result.output


def test_unknown_tool_fails_cleanly():
    result = registry.execute("does_not_exist")
    assert result.success is False
    assert "Unknown tool" in result.error


def test_missing_required_param_fails():
    result = registry.execute("read_file")
    assert result.success is False
    assert "Missing required parameters" in result.error


def test_search_files_on_temp_dir(tmp_path=Path("/tmp/medha_test")):
    tmp_path.mkdir(exist_ok=True)
    (tmp_path / "a.txt").write_text("hello")
    (tmp_path / "b.pdf").write_text("pdf-ish")
    result = registry.execute("search_files", root=str(tmp_path), pattern="*.pdf")
    assert result.success is True
    assert result.output["count"] == 1
    assert "b.pdf" in result.output["files"][0]


def test_high_risk_delete_requires_confirmation():
    from core.security_manager import security_manager
    security_manager.set_confirm_callback(lambda msg: False)
    test_file = Path("/tmp/medha_test/delete_me.txt")
    test_file.parent.mkdir(exist_ok=True)
    test_file.write_text("bye")
    result = registry.execute("delete_file", path=str(test_file))
    assert result.success is False
    assert test_file.exists()  # was NOT deleted because confirmation was denied


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
