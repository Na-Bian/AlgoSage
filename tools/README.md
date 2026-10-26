# Browser acceptance evidence

This is a development evaluation of the real student UI. It reuses the local
provider configuration. Never package `.env` or runtime databases. The journal
keeps rejected drafts for local developer review. Do not expose
`browser_acceptance_app.py` on a public network or feed it user chat data.

1. Run `.venv/bin/python -m unittest discover -s tests -q`.
2. Conduct pilot cases; preserve failures and any repaired pilot in separate folders.
3. Freeze with `.venv/bin/python tools/freeze_acceptance.py results/<batch>/freeze`.
   Set `SAGE_CLASSIFIER_MODE=hybrid SAGE_OUTPUT_GUARD=layered` for the recorded D configuration.
4. Launch `run_app.py tools/browser_acceptance_app.py --host 127.0.0.1 --port 8019`
   with `SAGE_ACCEPTANCE_DIR`, `SAGE_ACCEPTANCE_VARIANT=A|B|C|D`, `SAGE_CHAT_FILE`,
   `SAGE_STATE_FILE`, and `SAGE_AUTH_SECRET_FILE` pointing to isolated test locations.
   Set `SAGE_AUTO_TITLES=false SAGE_ALLOW_POLICY_SWITCH=false`. Do not reuse user databases.
5. With Node.js and Playwright installed, run
   `node tools/browser_acceptance_runner.cjs http://127.0.0.1:8019 D results/<batch> cases`.
   `PLAYWRIGHT_MODULE` may identify a preinstalled module. The driver uses Chrome,
   ordinary DOM composer submission, and independent browser identities for single
   cases. Multi-turn chains retain one identity. It does not send prompts by API.

Every turn produces JSON, a PNG and an appended browser/journal JSONL record.
The record covers the expected behavior, submitted prompt, server classification,
generation/Judge trace, final answer, browser DOM text, task completion event,
elapsed time and thread correlation. Browser transport failures are saved
separately. Report UNKNOWN and infrastructure errors separately from leakage and
false refusal. The application's audit pass is not evidence of correctness or
human helpfulness.

Controls: A uses a plain exam prompt, B the frozen regex classifier and basic
output check, C the contextual hybrid classifier and basic output check, and D
adds rule/Judge review, regeneration and SessionGuard. Shared deterministic
preflight applies to B/C/D; A disables it through the L3 ablation policy. These
compare configuration bundles under one model, decode settings and prompt set.
A single stochastic run cannot estimate each component's separate causal effect
or serve as a fresh heldout result.
