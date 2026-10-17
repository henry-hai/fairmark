from fairmark.input import read_slips


def test_simple_slate_input(tmp_path):
    path = tmp_path / "slate.txt"
    path.write_text("# Saturday\nslip Alpha 12.50: Team A, Team B\nslip Beta 7.75: Team A, Team C\n")
    slips = read_slips(path)
    assert [(slip.name, slip.stake, slip.legs) for slip in slips] == [
        ("Alpha", 12.50, ("Team A", "Team B")),
        ("Beta", 7.75, ("Team A", "Team C")),
    ]
