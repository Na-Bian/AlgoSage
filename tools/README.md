# Browser acceptance evidence

This is a development evaluation of the real student UI. It reuses the existing
local provider configuration; `.env` and runtime databases must never be packaged.
The journal intentionally includes rejected drafts for local developer review.
Never expose `browser_acceptance_app.py` on a public network or use user chat data.

1. Run `.venv/bin/python -m unittest discover -s tests -q` and
   `.venv/bin/python e2e_chainlit_check.py`.
2. Conduct pilot cases; preserve failures and any repaired pilot in separate folders.
3. Freeze with `.venv/bin/python tools/freeze_acceptance.py results/<batch>/freeze`.
   Set `EG_CLASSIFIER_MODE=hybrid EG_OUTPUT_GUARD=layered` for the recorded D configuration.
4. Launch `run_app.py tools/browser_acceptance_app.py --host 127.0.0.1 --port 8019`
   with `EG_ACCEPTANCE_DIR`, `EG_ACCEPTANCE_VARIANT=A|B|C|D`, `EG_CHAT_FILE`,
   `EG_STATE_FILE`, and `EG_AUTH_SECRET_FILE` pointing to isolated test locations.
   Set `EG_AUTO_TITLES=false EG_ALLOW_POLICY_SWITCH=false`. Do not reuse user databases.
5. With Node.js and Playwright installed, run
   `node tools/browser_acceptance_runner.cjs http://127.0.0.1:8019 D results/<batch> cases`.
   `PLAYWRIGHT_MODULE` may identify a preinstalled module. The driver uses Chrome,
   ordinary DOM composer submission, and independent browser identities for single
   cases. Multi-turn chains retain one identity. It does not send prompts by API.

Every turn produces JSON, a PNG and an appended browser/journal JSONL record. The
record includes the expected behavior, submitted prompt, server classification,
generation/Judge trace, final answer, browser DOM text, task completion event,
elapsed time and thread correlation. Browser transport failures are saved separately.
Classify UNKNOWN and infrastructure errors separately from leakage and false refusal.
Do not treat the application's own audit pass as correctness or human helpfulness.

Controls: A uses a plain exam prompt; B the frozen regex classifier/basic output
check; C the contextual hybrid classifier/basic output check; D adds rule/Judge
review, regeneration and SessionGuard. Shared deterministic preflight applies to
B/C/D; A disables it through the L3 ablation policy. These are configuration bundle
comparisons, not a clean causal estimate of every individual component. Same model,
decode settings and prompts; a single stochastic run, no fresh heldout claim.
