from netbox_librenms_plugin.acquisition import resources


def test_a_fixed_tick_list(monkeypatch):
    ticks = iter([0.0, 16.0])
    # ruleid: no-fixed-tick-list-for-a-clock
    monkeypatch.setattr(resources, "monotonic", lambda: next(ticks))


def test_a_fixed_tick_list_with_arguments(monkeypatch):
    ticks = iter([0.0, 16.0])
    # ruleid: no-fixed-tick-list-for-a-clock
    monkeypatch.setattr(resources, "perf_counter", lambda *args: next(ticks))


def test_a_mutable_clock(monkeypatch):
    clock = [0.0]
    # ok: no-fixed-tick-list-for-a-clock
    monkeypatch.setattr(resources, "monotonic", lambda: clock[0])


def test_the_budget_itself(monkeypatch):
    # ok: no-fixed-tick-list-for-a-clock
    monkeypatch.setattr(resources, "REMOTE_ALIAS_FETCH_BUDGET_SECONDS", 0.0)


def test_another_iterator(monkeypatch):
    names = iter(["a", "b"])
    # ok: no-fixed-tick-list-for-a-clock
    monkeypatch.setattr(resources, "next_name", lambda: next(names))
