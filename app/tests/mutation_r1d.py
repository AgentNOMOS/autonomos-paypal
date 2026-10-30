"""R1d mutation check: each mutant removes or weakens one R1d control; the R1d test suites must fail for every one.
Run from app/: python3 -B tests/mutation_r1d.py OUT.json        (R1c's tests/mutation_check.py covers payment code)
"""
import json
import os
import shutil
import subprocess
import sys
import tempfile

APP = os.path.abspath(os.path.join(os.path.dirname(__file__), ".."))
LAUNCHER = os.path.abspath(os.path.join(APP, "..", "live_demo", "owner_run_unified_demo.sh"))   # R1e: never skipped
V, L, G, P, RC, SV, E, EN, AC = ("autonomos/r1d/verifier.py", "autonomos/r1d/ledger.py", "autonomos/r1d/governed.py",
                                 "autonomos/r1d/providers.py", "autonomos/r1d/receipt2.py", "autonomos/r1d/service.py",
                                 "autonomos/r1d/envelope.py", "autonomos/r1d/engine.py", "autonomos/r1d/aiconfig.py")
COMPLETE = """            rec = self.provider.complete(system, user, schema, max_tokens=env.max_output_tokens_per_request,
                                         timeout_s=timeout_s, request_digest=body_digest)"""
MUTANTS = {
    "D01_quote_not_checked": (V, "elif nq not in norm(provided[anchor]) or nq not in docset.passages[anchor]:",
                              "elif False:"),
    "D02_source_membership_skipped": (V, "if src not in docset.files:", "if False:"),
    "D03_anchor_resolution_skipped": (V, "            if anchor not in docset.passages:\n",
                                      "            if False:\n"),
    "D04_unshown_anchor_allowed": (V, "elif anchor not in provided:", "elif False:"),
    "D05_finding_limit_ignored": (V, "if i >= maxf:", "if False:"),
    "D06_number_screen_off": (V, "    if missing:\n        reasons.append(f\"NUMBER_NOT_IN_QUOTE",
                              "    if False:\n        reasons.append(f\"NUMBER_NOT_IN_QUOTE"),
    "D07_negation_screen_off": (V, "if has_neg(claim) != has_neg(quote):", "if False:"),
    "D08_execution_screen_off": (V, "    if COMPLETED.search(claim) and (PLATFORM.search(claim) or GENERIC_HW.search("
                                    "claim)) and not has_neg(claim):\n        if not",
                                 "    if False:\n        if not"),
    "D09_flagged_source_not_withheld": (V, "if anchor in flagged:", "if False:"),
    "D10_lexical_threshold_off": (V, "if len(cs) < 2 or cov < 0.8:", "if False:"),
    "D11_payment_keys_unchecked": (V, "bad_keys = sorted(_keys(out) & FORBIDDEN_OUTPUT_KEYS)", "bad_keys = []"),
    "D12_ledger_execution_check_skipped": (L, 'if not r or r[0] != "STARTED" or r[2] != S.EXECUTING:',
                                           "if r is None: r = (None, None, None)\n            if False:"),
    "D13_ledger_capture_check_skipped": (L, 'if not cap or json.loads(cap[0]).get("verdict") != "CAPTURE_VERIFIED":',
                                         "if False:"),
    "D14_per_execution_limit_off": (L, "if n_exec >= env.max_requests_per_execution:", "if False:"),
    "D15_total_request_limit_off": (L, 'if t["requests"] + 1 > env.max_requests_total:\n                raise',
                                    "if False:\n                raise"),
    "D16_token_budget_off": (L, 'if t["tokens"] + est_input_tokens + env.max_output_tokens_per_request > '
                                'env.max_total_tokens:\n                raise', "if False:\n                raise"),
    "D17_usd_budget_off": (L, 'if env.max_usd_total is not None and t["usd"] + worst > env.max_usd_total:\n'
                              '                raise', "if False:\n                raise"),
    "D18_unknown_charged_zero": (L, 'charged = Decimal(row["worst_case_usd"])', "charged = Decimal(0)"),
    "D19_write_ahead_skipped": (G, "self.ledger.mark_sent(cid, body_digest, lease_s=timeout_s + LEASE_MARGIN_S)",
                                "pass"),
    "D20_retry_after_failure": (G, COMPLETE, "            try:\n    " + COMPLETE.replace("\n", "\n    ") +
                                "\n            except ProviderFailure:\n    " + COMPLETE.replace("\n", "\n    ")),
    "D21_mock_labelled_real": (P, "self._open, self.execution_class = transport, MOCK_EXECUTION",
                               "self._open, self.execution_class = transport, REAL_MODEL_EXECUTION"),
    "D22_label_check_disabled": (RC, 'checks["execution_label_consistent_with_evidence"] = _label_consistent(ai, '
                                     'artifact)', 'checks["execution_label_consistent_with_evidence"] = True'),
    "D23_collection_digest_not_rechecked": (SV, 'if ds.manifest_digest != offer["document_set"]["manifest_digest"]:',
                                            "if False:"),
    "D24_failed_verification_delivers": (SV, 'if ver["status"] == "FAILED":', "if False:"),
    "D25_server_side_fallbacks_on": (P, '"output_config": {"format": {"type": "json_schema", "schema": schema}, '
                                        '"effort": self.effort}}',
                                     '"output_config": {"format": {"type": "json_schema", "schema": schema}, '
                                     '"effort": self.effort}, "fallbacks": "default"}'),
    "D26_deadline_not_enforced": (P, "th.join(timeout_s + self.DEADLINE_GRACE_S)", "th.join()"),
    "D27_unknown_outcome_treated_as_rejected": (G, '.get(e.billing, "OUTCOME_UNKNOWN")', '.get(e.billing, "REJECTED")'),
    "D28_authorization_worst_case_unchecked": (E, "    if worst > usd:", "    if False:"),
    "D29_real_key_to_test_transport": (P, "            if api_key:\n                raise ProviderFailure(\"KEY_WITH",
                                       "            if False:\n                raise ProviderFailure(\"KEY_WITH"),
    "D30_payment_environment_unchecked": (L, "if payment_environment not in env.allowed_payment_environments:\n"
                                             "                raise", "if False:\n                raise"),
    "D31_api_key_env_tolerated": (AC, 'checks["anthropic_api_key_env_unset"] = "ANTHROPIC_API_KEY" not in e',
                                  'checks["anthropic_api_key_env_unset"] = True'),
    "D32_price_below_reference_accepted": (E, "if ref and (price_in < Decimal(ref[0]) or price_out < Decimal(ref[1])):",
                                           "if False:"),
    "D33_source_injection_flag_off": (EN, '    if flagged:\n        flags.append("INSTRUCTION_LIKE_TEXT_IN_SOURCES")',
                                      '    if False:\n        flags.append("INSTRUCTION_LIKE_TEXT_IN_SOURCES")'),
    "D34_legacy_ai_selector_allowed": (SV, 'if cfg.ai_provider != "fixture":', "if False:"),
    "D35_strict_json_relaxed": (EN, "    if set(obj) != want:", "    if not want <= set(obj):"),
    "D36_expiry_unchecked": (L, "if env.not_after is not None and now > env.not_after:\n                raise",
                             "if False:\n                raise"),
    "D37_capacity_gate_off": (SV, "            if not ok:\n                self.store.note(tid, \"policy\"",
                              "            if False:\n                self.store.note(tid, \"policy\""),
    "D38_preflight_prompt_fit_unchecked": ("autonomos/r1d/preflight.py",
                                           'checks["envelope_fits_largest_prompt"] = env.max_input_tokens_per_request '
                                           '>= need', 'checks["envelope_fits_largest_prompt"] = True'),
    "D39_capacity_ignores_request_total": (L, '        if t["requests"] + 1 > env.max_requests_total:\n            '
                                              'return False, "REQUEST_LIMIT_TOTAL"',
                                           '        if False:\n            return False, "REQUEST_LIMIT_TOTAL"'),
    "D40_real_demo_without_owner_confirm": ("autonomos/__main__.py",
                                            'if "--owner-confirm" not in argv:\n                raise SystemExit("refused: a REAL',
                                            'if False:\n                raise SystemExit("refused: a REAL'),
    "D41_real_runtime_any_database": (SV, "(not bound or os.path.realpath(cfg.data_dir) != bound):", "False:"),
    "D42_data_dir_check_off": (AC, '    checks["data_dir_matches_authorization"] = bool(env) and bool(dd) and \\\n',
                               '    checks["data_dir_matches_authorization"] = True or \\\n'),
    # added 2026-10-17 with the D18 gap test: the recovery path's write of an unknown outcome's charge
    "D43_recovered_unknown_charged_zero": (L, '(worst, "interrupted: process ended', '("0", "interrupted: process ended'),
    # added 2026-10-17 in R1d.1 (Console API key families)
    "D44_any_sk_ant_token_accepted": (AC, '    return False, "not an accepted Console API key family (sk-ant-apiNN-, sk-ant-usr-)"',
                                      '    return k.startswith("sk-ant-"), "widened"'),
    "D45_named_token_refusals_removed": (AC, "    for prefix, why in REFUSED_KEY_PREFIXES:", "    for prefix, why in ():"),
    "D46_key_format_result_ignored": (AC, '        checks["key_file_format"] = ok', '        checks["key_file_format"] = True'),
    "D47_key_family_prefix_match_only": (AC, "        if rx.fullmatch(k):", "        if rx.match(k):"),
    # added 2026-10-17 in R1d.2 (evidence intelligence): each mutant weakens one new rule
    "D48_context_whole_item_not_sentence": (V, "        ss = sentences(item)\n", "        ss = [item]\n"),
    "D49_zero_job_row_counts_as_execution": (V, "if m and int(m.group(1)) >= 1:", "if m and int(m.group(1)) >= 0:"),
    "D50_unit_negation_plan_gate_off": (V, "    if has_neg(text) or PLAN2.search(text) or EXEC_NEGATOR.search(text):\n"
                                           "        return False, set(), None",
                                        "    if False:\n        return False, set(), None"),
    "D51_context_platform_unchecked": (V, "if est and _platform_ok(platforms(claim), ps):", "if est:"),
    "D52_contradiction_scan_off": (V, "        for text, dps in denials:", "        for text, dps in ():"),
    "D53_context_numbers_accepted": (V, "    missing = sorted(numbers2(claim) - numbers2(quote))",
                                     "    missing = sorted(numbers2(claim) - numbers2(quote + ' ' + (unit or {}).get("
                                     "'text', '')))"),
    "D54_identifier_screen_off": (V, "    if missing_ids:\n", "    if False:\n"),
    "D55_interval_check_off": (V, "        if (i[\"value\"], i[\"level\"], i[\"low\"], i[\"high\"]) not in keyed:",
                               "        if False:"),
    "D56_unit_mismatch_off": (V, "            if qunits - {None}:\n                hard.append",
                              "            if False:\n                hard.append"),
    "D57_percent_drop_allowed": (V, "if \"percent\" in qunits and None not in qunits and o[\"unit\"] != \"percent\":",
                                 "if False:"),
    "D58_qualifier_check_off": (V, "        if o[\"qualifier\"] not in qquals:", "        if False:"),
    "D59_column_attribution_off": (V, "            if not ok_any:\n                hard.append",
                                   "            if False:\n                hard.append"),
    "D60_raw_evidence_screen_off": (V, "    if RAW_AVAIL.search(claim) and not has_neg(claim)",
                                    "    if False and RAW_AVAIL.search(claim) and not has_neg(claim)"),
    "D61_free_text_whole_not_sentences": (V, "    for s in sentences(text):", "    for s in [text]:"),
    "D62_summary_backing_ignores_platform": (V, "            if not any(_platform_ok(ps, b) for b in (exec_backing or [])):",
                                             "            if not exec_backing:"),
    "D63_research_completeness_always_pass": (V, '("PARTIAL" if rc_reasons else "PASS")', '"PASS"'),
    "D64_soft_numeric_reasons_ignored": (V, "        if n_soft:\n", "        if False:\n"),
    "D65_recheck_ignores_context_unit": (V, "        if ctx and (ctx[\"passage_id\"] != e[\"passage_id\"]",
                                         "        if False and (ctx[\"passage_id\"] != e[\"passage_id\"]"),
    "D66_derived_completeness_always_pass": ("autonomos/r1d/summary.py",
                                                 '    value = "FAIL" if v.get("status") == "FAILED" or not '
                                                 'c.get("claim_supported") else (\n        "PARTIAL" if reasons else "PASS")',
                                                 '    value = "PASS"'),
    # added in R1e (unified workflow): each mutant weakens one R1e rule
    'E01_execute_capture_gate_off': (SV, 'if not cap or cap["verification"].get("verdict") != "CAPTURE_VERIFIED":\n            raise GateError("no verified capture on record")', 'if False:\n            raise GateError("no verified capture on record")'),
    'E02_recording_question_unchecked': ('autonomos/r1d/recorded.py', 'if not m or m.group(1) != self.meta["question"]:', 'if False:'),
    'E03_recording_passages_unchecked': ('autonomos/r1d/recorded.py', 'if list(shown) != self.meta["passage_ids"] or hashlib.sha256(', 'if False and hashlib.sha256('),
    'E04_recording_answers_twice': ('autonomos/r1d/recorded.py', 'if not nonce or nonce.group(1) in self.answered:', 'if False:'),   # R1e.1: once per request
    'E05_recording_digest_unchecked': ('autonomos/r1d/recorded.py', 'if hashlib.sha256(self.raw).hexdigest() != self.meta["raw_output_sha256"]:', 'if False:'),
    'E06_replay_label_hidden': ('autonomos/r1d/recorded.py', 'self.label = (f"{REPLAY_PREFIX} of', 'self.label = (f"https://api.anthropic.com/v1/messages of'),
    'E07_replay_ids_look_real': ('autonomos/r1d/recorded.py', '"id": f"REPLAY-{self.meta', '"id": f"{self.meta'),
    'E08_question_as_finding_allowed': (V, '    if ("?" in quote or (unit and "?" in unit["text"])) and not QUESTION_FRAME.search(claim):', '    if False:'),
    'E09_polarity_flip_allowed': (V, '    if flips:\n', '    if False:\n'),
    'E10_entity_check_off': (V, '    if missing:\n        hard.append(f"ENTITY_NOT_IN_EVIDENCE', '    if False:\n        hard.append(f"ENTITY_NOT_IN_EVIDENCE'),
    'E11_negation_scope_off': (V, '        if h and q_heads and not any(h & qh for qh in q_heads):', '        if False:'),
    'E12_strength_escalation_off': (V, '    if strong:\n', '    if False:\n'),
    'E13_condition_drop_allowed': (V, '    if CONDITIONAL.search(quote) and not CONDITION_FRAME.search(claim):', '    if False:'),
    'E14_plan_drop_allowed': (V, '    if PLAN_WORDS.search(quote) and not PLAN_FRAME.search(claim):', '    if False:'),
    'E15_unconfirmed_mixed_into_findings': (V, '"findings": [d for d in delivered if d["claim_status"] == "CLAIM_SUPPORTED"],', '"findings": delivered,'),
    'E16_recheck_skips_unconfirmed': (V, 'artifact["report"]["findings"] + artifact["report"].get("unconfirmed_findings", [])', 'artifact["report"]["findings"]'),
    'E17_truth_check_skips_unconfirmed': (RC, '\n            + artifact.get("report", {}).get("unconfirmed_findings", []))', ')'),
    'E18_payment_environment_preflight_off': (AC, '    checks["payment_environment_allowed"] = bool(env) and penv in env.allowed_payment_environments', '    checks["payment_environment_allowed"] = True'),
    'E19_buyer_status_panel_removed': ('autonomos/server.py', '+ ([self.status_panel(v)] if getattr(svc, "runtime", None) else [])', '+ []'),
    # added in R1e.1 (hackathon demo gate): each mutant weakens one R1e.1 rule
    'F01_count_conflict_allowed': (V, '        if e_words:\n            hard.append(f"COUNT_CHANGED', '        if False:\n            hard.append(f"COUNT_CHANGED'),
    'F02_negated_counts_counted': (V, '\n            and not any(x in NEG or x.endswith("n\'t") for x in ws[max(0, i - 2):i])}', '}'),
    'F03_provenance_escalation_soft': (V, '        if REPORTED_FRAME.search(span):', '        if False:'),
    'F04_provenance_check_off': (V, '    if prov:\n', '    if False:\n'),
    'F05_contrast_swap_allowed': (V, '            if ca & sb and cb & sa:', '            if False:'),
    'F06_rejected_side_allowed': (V, '            if cs & sb and len(cs & sb) / len(sb) > (len(cs & sa) / len(sa) if sa else 0):', '            if False:'),
    'F07_inference_allowed': (V, '    if inf:\n', '    if False:\n'),
    'F08_comparison_allowed': (V, '    if comp:\n', '    if False:\n'),
    'F09_temporal_allowed': (V, '    if temp:\n', '    if False:\n'),
    'F10_transform_not_wired': (V, '        t_hard, t_soft = transform_check(claim, nq, unit)', '        t_hard, t_soft = [], []'),
    'F11_replay_mode_any_payment': ('autonomos/r1d/demo_mode.py', 'if cfg.payment_provider != "mock":', 'if False:'),
    'F12_replay_mode_any_ai': ('autonomos/r1d/demo_mode.py', 'if env.execution_class != MOCK_EXECUTION or not _is_replay(runtime):', 'if False:'),
    'F13_live_without_marker': ('autonomos/r1d/demo_mode.py', 'if not marker or not os.path.isdir(marker):', 'if False:'),
    'F14_live_without_caps': ('autonomos/r1d/demo_mode.py', 'if cfg.sandbox_max_orders != 1 or cfg.sandbox_max_capture_requests != 1:', 'if False:'),
    'F15_live_authorization_any_env': ('autonomos/r1d/demo_mode.py', 'if tuple(env.allowed_payment_environments) != ("SANDBOX",):', 'if False:'),
    'F16_live_without_sandbox_go': ('autonomos/r1d/demo_mode.py', 'if cfg.sandbox_owner_go != "capture-at-most-1-order":', 'if False:'),
    'F17_serve_mode_gate_removed': ('autonomos/__main__.py', '            svc.demo_mode = resolve(cfg, svc.runtime)', '            svc.demo_mode = {"mode": "DEVELOPMENT", "banner": "", "live": False}'),
    'F18_replay_question_guard_off': ('autonomos/server.py', 'if dm.get("mode") == "DEMO_REPLAY" and (f.get("topic") != dm["question"] or', 'if False and (f.get("topic") != dm["question"] or'),
    'F19_receipt_row_always_valid': ('autonomos/server.py', "{'VALID' if vr['valid'] else 'INVALID'}", 'VALID'),
    'F20_mode_banner_removed': ('autonomos/server.py', "<main>{mb}<p class='banner'>", "<main><p class='banner'>"),
    'F21_authorization_row_always_authorized': ('autonomos/server.py', '            if S.EXECUTION_AUTHORIZED in ts:', '            if True:'),
    'F22_live_any_runtime': ('autonomos/r1d/demo_mode.py', '    if env.execution_class != REAL_MODEL_EXECUTION:', '    if False:'),
}


def run(name):
    tmp = tempfile.mkdtemp(prefix="autonomos_r1d_mut_")
    shutil.copytree(os.path.join(APP, "autonomos"), os.path.join(tmp, "autonomos"))
    shutil.copytree(os.path.join(APP, "tests"), os.path.join(tmp, "tests"))
    os.symlink(os.path.join(APP, "corpus"), os.path.join(tmp, "corpus"))
    os.symlink(os.path.join(APP, "recordings"), os.path.join(tmp, "recordings"))           # R1e
    if name in MUTANTS:
        f, old, new = MUTANTS[name]
        p = os.path.join(tmp, f)
        s = open(p).read()
        if s.count(old) != 1:
            return {"error": f"anchor found {s.count(old)} times"}
        open(p, "w").write(s.replace(old, new))
    r = subprocess.run([sys.executable, "-B", "-W", "ignore", "-m", "unittest", "discover", "-s", "tests", "-p",
                        "test_r1[de]*.py"], cwd=tmp, capture_output=True, text=True, timeout=900,
                       env=dict(os.environ, AUTONOMOS_AI_PROVIDER="fixture", AUTONOMOS_PAYMENT_PROVIDER="mock",
                                AUTONOMOS_R1E_LAUNCHER=LAUNCHER))
    shutil.rmtree(tmp, ignore_errors=True)
    fails = [l for l in r.stderr.splitlines() if l.startswith(("FAIL:", "ERROR:"))]
    return {"killed": r.returncode != 0, "failing": fails[:6], "tail": r.stderr.strip().splitlines()[-1:]}


if __name__ == "__main__":
    res = {"M0_control": run("M0_control")}
    print("M0_control", res["M0_control"])
    only = sys.argv[2:]                              # R1e.1: optional name prefixes for a targeted run
    for m in MUTANTS:
        if only and not m.startswith(tuple(only)):
            continue
        res[m] = run(m)
        print(m, res[m].get("killed"), res[m].get("error"), res[m].get("failing", [])[:2], flush=True)
    out = {"control_passes": not res["M0_control"]["killed"],
           "killed": sum(1 for k, v in res.items() if k != "M0_control" and v.get("killed")),
           "mutants": len(res) - 1, "selection": sys.argv[2:] or "all",
           "anchor_errors": [k for k, v in res.items() if v.get("error")], "results": res}
    json.dump(out, open(sys.argv[1], "w"), indent=1)
    print("control passes:", out["control_passes"], "killed", out["killed"], "/", out["mutants"])
