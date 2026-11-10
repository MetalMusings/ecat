from ecatsetup import diagnose as d


def port(n, lost):
    return d.Port(n, True, {"rx": 0, "phy": 0, "fwd": 0, "lost": lost},
                  {"rx": 0, "phy": 0, "fwd": 0, "lost": 0})


def report(pos, ports):
    return d.Report(pos, "x", False, 8, False, 0, ports=ports)


def test_same_lost_count_at_both_ends_names_the_cable():
    notes = d.lost_link_notes([report(0, [port(0, 255), port(1, 1)]), report(1, [port(0, 1), port(1, 0)])])
    assert len(notes) == 1 and "slave 0 port 1 and slave 1 port 0" in notes[0]


def test_saturated_or_unequal_counts_say_nothing():
    assert d.lost_link_notes([report(0, [port(1, 255)]), report(1, [port(0, 255)])]) == []
    assert d.lost_link_notes([report(0, [port(1, 1)]), report(1, [port(0, 2)])]) == []
