from geobench.parse_logs import assign_requests


def run(run_id, start, end, variant="legacy"):
    return {"run_id": run_id, "variant": variant, "start": start, "end": end}


def req(ts, path="/", ua="curl/8", host="legacy.example.test"):
    return {"ts": ts, "host": host, "variant": host.split(".")[0], "path": path, "ua": ua}


RUNS = [
    run("r1", "2026-11-09T14:00:00.000Z", "2026-11-09T14:01:00.000Z"),
    run("r2", "2026-11-09T14:02:00.000Z", "2026-11-09T14:03:00.000Z"),
]


def test_request_inside_window_is_assigned():
    result = assign_requests(RUNS, [req("2026-11-09T14:00:30.000Z")])
    assert len(result.by_run["r1"]) == 1 and not result.unassigned


def test_request_in_tail_after_end_is_assigned():
    result = assign_requests(RUNS, [req("2026-11-09T14:01:20.000Z")])
    assert len(result.by_run["r1"]) == 1


def test_request_in_gap_is_unassigned_and_a_plain_client_voids_nothing():
    result = assign_requests(RUNS, [req("2026-11-09T14:01:45.000Z")])
    assert len(result.unassigned) == 1 and not result.invalid_runs


def test_late_agent_request_voids_nearest_earlier_run():
    result = assign_requests(RUNS, [req("2026-11-09T14:01:45.000Z", ua="Claude-User/1.0")])
    assert result.invalid_runs == {"r1"}


def test_other_variant_host_does_not_match():
    result = assign_requests(RUNS, [req("2026-11-09T14:00:30.000Z", host="ai.example.test")])
    assert len(result.unassigned) == 1


def test_agent_request_before_any_run_voids_nothing():
    result = assign_requests(RUNS, [req("2026-11-09T13:00:00.000Z", ua="GPTBot/1.1")])
    assert not result.invalid_runs


def test_request_just_before_run_start_belongs_to_that_run_not_the_previous_tail():
    # clock-offset jitter put the first request 1 s before start. The previous run's tail must not take it.
    runs = [run("r1", "2026-11-09T14:00:00.000Z", "2026-11-09T14:01:00.000Z"),
            run("r2", "2026-11-09T14:01:31.000Z", "2026-11-09T14:02:00.000Z")]
    result = assign_requests(runs, [req("2026-11-09T14:01:30.000Z")])
    assert len(result.by_run["r2"]) == 1 and not result.by_run["r1"]


def token_run(run_id, token, start, end, variant="legacy"):
    return run(run_id, start, end, variant) | {"run_token": token}


def token_req(ts, token, variant="legacy", ua="curl/8"):
    return {"ts": ts, "host": f"{token}.{variant}.example.test", "run_token": token, "variant": variant, "path": "/", "ua": ua}


TOKEN_RUNS = [
    token_run("r1", "aaaaaaaa", "2026-11-09T14:00:00.000Z", "2026-11-09T14:01:00.000Z"),
    token_run("r2", "bbbbbbbb", "2026-11-09T14:00:30.000Z", "2026-11-09T14:01:30.000Z"),  # windows overlap on purpose
]


def test_token_assigns_exactly_even_when_windows_overlap():
    result = assign_requests(TOKEN_RUNS, [token_req("2026-11-09T14:00:40.000Z", "aaaaaaaa"), token_req("2026-11-09T14:00:41.000Z", "bbbbbbbb")])
    assert len(result.by_run["r1"]) == 1 and len(result.by_run["r2"]) == 1
    assert not result.unassigned and not result.outside_window


def test_known_token_outside_the_window_is_assigned_and_flagged_not_voided():
    result = assign_requests(TOKEN_RUNS, [token_req("2026-11-09T15:00:00.000Z", "aaaaaaaa", ua="Claude-User/1.0")])
    assert len(result.by_run["r1"]) == 1 and not result.unassigned
    assert result.outside_window == {"r1"} and not result.invalid_runs


def test_unknown_token_is_unassigned_and_voids_no_run_even_for_an_agent():
    result = assign_requests(TOKEN_RUNS, [token_req("2026-11-09T14:00:40.000Z", "zzzzzzzz", ua="Claude-User/1.0")])
    assert len(result.unassigned) == 1 and not result.invalid_runs
    assert not any(result.by_run.values())


def test_token_on_the_wrong_variant_is_unassigned():
    result = assign_requests(TOKEN_RUNS, [token_req("2026-11-09T14:00:40.000Z", "aaaaaaaa", variant="ai")])
    assert len(result.unassigned) == 1 and not result.by_run["r1"]


def test_a_tokenless_request_never_lands_in_a_run_that_has_a_token():
    # a scanner on the bare variant host, inside the window of r1
    scan = {"ts": "2026-11-09T14:00:10.000Z", "host": "legacy.example.test", "run_token": None, "variant": "legacy", "path": "/.git/config", "ua": "Mozilla/5.0"}
    result = assign_requests(TOKEN_RUNS, [scan])
    assert result.unassigned == [scan] and not any(result.by_run.values())


def test_a_tokenless_agent_request_voids_no_run_that_has_a_token():
    agent = {"ts": "2026-11-09T14:05:00.000Z", "host": "legacy.example.test", "run_token": None, "variant": "legacy", "path": "/", "ua": "Claude-User/1.0"}
    result = assign_requests(TOKEN_RUNS, [agent])
    assert result.unassigned == [agent] and not result.invalid_runs


def test_tokenless_runs_in_the_same_batch_still_use_the_window():
    shared = run("s1", "2026-11-09T16:00:00.000Z", "2026-11-09T16:01:00.000Z")  # no run_token: shared mode
    plain = {"ts": "2026-11-09T16:00:10.000Z", "host": "legacy.example.test", "run_token": None, "variant": "legacy", "path": "/", "ua": "curl/8"}
    result = assign_requests(TOKEN_RUNS + [shared], [plain])
    assert len(result.by_run["s1"]) == 1 and not result.unassigned
