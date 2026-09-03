"""Single model-call boundary. A future API adapter can implement evaluate_once's signature."""

import json
from pathlib import Path
import re


def evaluate_once(*, packet, image_paths, system_prompt, schema, workdir, timeout):
    """Return (annotation, metadata); retain raw response/logs, raise on invocation failure.

    packet is JSON-serializable; image_paths contains local original images in attachment order.
    system_prompt/schema are frozen files. No API endpoint or credential assumptions are made here.
    """
    from analysis.scripts.step_08_03_pilot_visual_context_vlm import command, run_process, digest
    workdir = Path(workdir)
    raw = workdir / '09_model_raw.json'
    args = command(workdir, image_paths, raw, Path(system_prompt), Path(schema))
    (workdir / '09_invocation.json').write_text(json.dumps({
        'backend': 'codex', 'argv': args, 'prompt_sha256': digest(system_prompt),
        'schema_sha256': digest(schema)}, ensure_ascii=False, indent=2) + '\n')
    with (workdir / '09_stdout.log').open('w') as out, (workdir / '09_stderr.log').open('w') as err:
        code = run_process(args, 'Assess this PR evidence packet once.\n' +
                           json.dumps(packet, ensure_ascii=False), out, err, timeout)
    if code:
        raise ValueError(f'Model process exited {code}; inspect 09_stderr.log')
    log = (workdir / '09_stderr.log').read_text()
    model = re.search(r'^model: (.+)$', log, re.M)
    effort = re.search(r'^reasoning effort: (.+)$', log, re.M)
    if not model or model[1] != 'gpt-5.6-luna' or not effort or effort[1] != 'max':
        raise ValueError('Requested Luna/max not confirmed; no automatic fallback')
    tokens = re.search(r'tokens used\s*\n([\d,]+)', log)
    return json.loads(raw.read_text()), {
        'backend': 'codex', 'model': model[1], 'effort': effort[1],
        'raw_response': str(raw), 'raw_response_sha256': digest(raw),
        'cli_reported_tokens': int(tokens[1].replace(',', '')) if tokens else None}
