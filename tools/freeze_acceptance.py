"""Freeze public source and whitelisted runtime configuration without secrets."""
import hashlib
import importlib.metadata
import json
import os
from pathlib import Path
import platform
import subprocess
import sys
import zipfile
from datetime import datetime, timezone

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
from examguard.config import Settings


def public_sources():
    excluded = {'.git', '.venv', '.examguard', '.files', '.playwright-cli', '.pytest_cache',
                '.ruff_cache', '__pycache__', 'results', 'node_modules'}
    for folder, dirs, files in os.walk(ROOT):
        dirs[:] = sorted(d for d in dirs if d not in excluded)
        for name in sorted(files):
            p = Path(folder) / name
            if name == '.env' or (name.startswith('.env.') and name != '.env.example') or name.endswith(('.pyc', '.log', '.sqlite3', '.secret')) or name == '.DS_Store':
                continue
            yield p


def main():
    destination = Path(sys.argv[1]).resolve()
    destination.mkdir(parents=True, exist_ok=True)
    settings = Settings.from_env()
    safe_names = ('mode', 'base_url', 'model', 'exam_mode', 'timeout', 'max_output_tokens',
                  'token_parameter', 'classifier_mode', 'output_guard', 'extra_body',
                  'question_id', 'max_calls_per_turn', 'turn_timeout',
                  'classifier_max_output_tokens', 'judge_max_output_tokens', 'min_classifier_confidence')
    # extra_body is provider-specific; only approved scalar decode settings leave.
    safe = {k:getattr(settings, k) for k in safe_names if k != 'extra_body'}
    safe['extra_body'] = {k:v for k,v in settings.extra_body.items() if k in {'enable_thinking', 'temperature', 'top_p', 'seed'}}
    safe.update(auto_titles=False, allow_policy_switch=False, isolated_browser_identities=True,
                dataset_scope='public development acceptance; one stochastic run, not blind heldout')
    (destination/'runtime_config.json').write_text(json.dumps(safe, ensure_ascii=False, indent=2))
    sources = list(public_sources())
    manifest = {str(p.relative_to(ROOT)): hashlib.sha256(p.read_bytes()).hexdigest() for p in sources}
    # Even an accidentally saved exact local key must not enter a source archive.
    if settings.api_key and any(settings.api_key.encode() in p.read_bytes() for p in sources):
        raise SystemExit('Secret found in public source; freeze aborted')
    metadata = dict(at=datetime.now(timezone.utc).isoformat(), python=platform.python_version(),
                    platform=platform.platform(), git_head=subprocess.check_output(['git','rev-parse','HEAD'],cwd=ROOT,text=True).strip(),
                    dirty=True, source_sha256=manifest)
    (destination/'source_manifest.json').write_text(json.dumps(metadata, ensure_ascii=False, indent=2))
    versions = sorted({f"{d.metadata['Name']}=={d.version}" for d in importlib.metadata.distributions() if d.metadata.get('Name')})
    (destination/'environment.lock.txt').write_text('\n'.join(versions)+'\n')
    with zipfile.ZipFile(destination/'source.zip', 'w', zipfile.ZIP_DEFLATED) as archive:
        for p in sources: archive.write(p, 'ExamGuard/'+str(p.relative_to(ROOT)))
    for p in (destination/'source.zip', destination/'runtime_config.json', destination/'source_manifest.json', destination/'environment.lock.txt'):
        print(hashlib.sha256(p.read_bytes()).hexdigest(), p.name)


if __name__ == '__main__': main()
