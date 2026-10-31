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


def test_a_fixed_tick_list_with_more_setattr_arguments(monkeypatch):
    ticks = iter([0.0, 16.0])
    # ruleid: no-fixed-tick-list-for-a-clock
    monkeypatch.setattr(resources, "monotonic", lambda: next(ticks), raising=False)
    # ruleid: no-fixed-tick-list-for-a-clock
    monkeypatch.setattr(resources, "time_ns", lambda *args: next(ticks), raising=False)


def test_a_fixed_tick_list_on_a_dotted_target(monkeypatch):
    ticks = iter([0.0, 16.0])
    # ruleid: no-fixed-tick-list-for-a-clock
    monkeypatch.setattr("time.monotonic", lambda: next(ticks))
    # ruleid: no-fixed-tick-list-for-a-clock
    monkeypatch.setattr("time.perf_counter", lambda *args: next(ticks), raising=False)


def test_another_iterator_on_a_dotted_target(monkeypatch):
    names = iter(["a", "b"])
    # ok: no-fixed-tick-list-for-a-clock
    monkeypatch.setattr("module.counter", lambda: next(names))


def test_a_mutable_clock_on_a_dotted_target(monkeypatch):
    clock = [0.0]
    # ok: no-fixed-tick-list-for-a-clock
    monkeypatch.setattr("time.monotonic", lambda: clock[0], raising=False)
