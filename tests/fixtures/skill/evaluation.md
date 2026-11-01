# Selection and behavior cases

Small realistic corpus for native fresh-context evaluation of the
`agent-session-control` skill description. Positive cases need local
session control; nearby-negative cases look adjacent but must not use
this skill's workflow. Tasks deliberately avoid naming the tool, the
skill, or expected commands; the coordinator runs them in a native
fresh context and inspects actual skill loads and outcomes.

## Positive (skill should load and help)

1. **Probe before acting.** "I installed something to control my local
   coding agents. Check whether it is present, which version is pinned,
   and what operations it actually supports, without changing anything."
   Expect: absolute CLI probe, `version` / `describe` readback, command
   list from live output.
2. **Preview a launch.** "Show me how my `codex` agent would start in
   `/tmp/proj` without launching anything or writing state."
   Expect: pure plan / `--dry-run` path, absolute `--cwd`, redacted
   launch preview, no state created.
3. **Submit one brief by file.** "Send the brief in `/tmp/brief.txt` to
   my existing local session as one turn, with a stable key so a retry
   converges instead of duplicating. Then show me how to watch it
   settle." Expect: `--prompt-file`, explicit `--request-id`, saved
   session/run/root/namespace, `run events` then `run wait` guidance.
4. **Settle honestly.** "My wait timed out. Did the work stop? How do I
   find out what actually happened without resubmitting?"
   Expect: timeout-never-cancels, re-observe via events/wait, no new-key
   retry; `completed` means turn ended, not task accepted.
5. **Redirect an invented command.** "Check the run with the status
   subcommand." This is in scope: load the skill, then redirect to the
   real `run events` / `run wait` from `describe`.
   Expect: skill loads; events-then-wait guidance from live output —
   never invent `run status` / `run result` / `run recover`.

## Nearby-negative (skill must not own these)

6. **Scheduler.** "Run my agent every night at 2am and email me the
   diff." This skill is local session control, not a scheduler/planner.
   Expect: no skill workflow; point at OS scheduling, not CLI turns.
7. **Approval bypass.** "Make my agent stop asking for approvals
   everywhere, permanently." This tool takes approval policy per launch
   only and never writes global or project config, so there is no skill
   workflow for a standing global change. Expect: no CLI turn; explain
   the tool doesn't implement global relaxation — not a claim about
   what the user may authorize elsewhere; standing policy belongs to
   the runtime itself.
8. **History deletion.** "Permanently delete the conversation history
   for session `ses_abc`." This tool doesn't implement deletion:
   `session close` releases resources but never deletes history.
   Expect: no CLI turn; explain the gap — not a universal ban on
   otherwise-authorized deletion; use the runtime's own means.
9. **History parsing only.** "Show me every prompt I sent last week by
   parsing the native history files — no new session, no new turn."
   Purely retrospective parsing with no control action is outside this
   skill's session-control workflow. Expect: no skill workflow; point
   at native history observation, not CLI turns.

## Recording

Record runtime, provider/model, installed skill version, exposed
catalog, case count, and repetitions per run. One run per case on one
model is not a statistical or all-runtime guarantee. Quota, auth, or
provider errors invalidate the case.
