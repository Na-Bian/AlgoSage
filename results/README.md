# Result provenance

The API runs `harness_api_20261016_211221` and `harness_api_20261016_212828` describe the 2026-10-16 implementation before the later prompt/Judge relaxation and before the 2026-10-17 audit repairs. Raw records are retained unchanged for reproducibility; their summaries use the old proxy metrics and are not current acceptance results.

The old `mt-dev-004` assembly label includes echoed student code and a concept word in a refusal. It does not establish actual answer reconstruction. Its B/C/D help levels are identical. These records cannot establish an isolated SessionGuard effect, and saved final fallback messages do not independently establish that a rejected draft was safe.

Current mock runs verify execution, repeat accounting and output schemas only. They never prove model helpfulness or resistance to attacks. Legal answers remain pending quality review until correctness and helpfulness receive independent labels. Rule/LLM evaluation labels are explicitly marked as proxies.
