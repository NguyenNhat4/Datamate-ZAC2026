"""Local interface for running and inspecting tabular-agent questions."""
from datetime import datetime
import json
from pathlib import Path
import subprocess
import sys
import tempfile

import httpx

import streamlit as st
from tabular_agent.contracts import read_jsonl
from tabular_agent.model import create_model, openai_api_key, openai_model_name

st.set_page_config(page_title='Agent test lab', page_icon='🔎', layout='wide')
st.title('Agent test lab')
st.caption('Choose questions, run the agent, and inspect its actions.')

split = st.text_input('Dataset directory', value='data/train')
provider = st.radio('Model provider', ['Local server', 'OpenAI API'], horizontal=True)
if provider == 'OpenAI API':
    model = st.text_input('OpenAI model name', value=openai_model_name())
    base_url = None
    st.info('OpenAI receives the question, business instructions, generated code and observations. API usage is billed separately and is unsuitable for offline-only evaluation.')
    if not openai_api_key():
        st.warning('Set OPENAI_API_KEY in your environment or local .env file before running.')
else:
    model = st.text_input('Local model name', value='local-coder-7b')
    base_url = st.text_input('Local model endpoint', value='http://127.0.0.1:8000/v1')
    st.caption('Start an OpenAI-compatible local model server separately and keep it running. The UI does not start it.')
    if st.button('Check local connection'):
        try:
            # Reuse the runner's loopback validation and HTTP client settings.
            local_model = create_model(model, base_url, timeout=3)
            try:
                response = local_model.client.get('/models', cast_to=dict[str, object])
            finally:
                local_model.client.close()
            available = [item.get('id') for item in response.get('data', []) if isinstance(item, dict)] if isinstance(response, dict) else []
            if available and model not in available:
                st.warning(f'Server is reachable, but {model!r} was not in its model list: {available}')
            else:
                st.success('Local server is reachable.' + (' The model name is available.' if available else ' Check that the model name matches the server.'))
        except (ValueError, httpx.HTTPError) as error:
            st.error(f'Cannot check local server: {error}')
        except Exception as error:
            st.error(f'Cannot check local server: {type(error).__name__}: {error}')
telemetry = st.checkbox('Send traces to local Phoenix', value=False,
                        help='Start Phoenix first with make telemetry; traces can include business data.')
if telemetry:
    st.caption('Run make telemetry first, then open Phoenix to inspect the traces.')
    st.link_button('Open Phoenix', 'http://127.0.0.1:6006')
try:
    questions = read_jsonl(Path(split) / 'questions.jsonl')
except (OSError, ValueError) as error:
    st.info(f'Choose a valid dataset directory: {error}')
    st.stop()

lookup = {q['id']: q for q in questions}
businesses = sorted({q['business_id'] for q in questions if isinstance(q.get('business_id'), str)})
if not businesses:
    st.info('No businesses with questions were found in this dataset.')
    st.stop()

business = st.selectbox('Business', businesses)
business_questions = {q['id']: q for q in questions if q.get('business_id') == business}
chosen = st.multiselect(
    'Questions', list(business_questions),
    format_func=lambda key: f"{key} — {business_questions[key]['question']}",
    key=f'questions:{Path(split).resolve()}:{business}',
)
if st.button('Run selected questions', disabled=not chosen):
    root = Path('runs') / ('ui-' + datetime.now().strftime('%Y%m%d-%H%M%S-%f'))
    command = [sys.executable, '-m', 'tabular_agent.cli',
               'bench' if (Path(split) / 'answers.jsonl').is_file() else 'predict',
               '--split', split, '--output', str(root), '--limit', '0',
               '--provider', 'openai' if provider == 'OpenAI API' else 'local', '--model', model]
    if base_url:
        command.extend(['--base-url', base_url])
    for question_id in chosen:
        command.extend(['--question-id', question_id])
    if telemetry:
        command.append('--telemetry')
    log = tempfile.TemporaryFile(mode='w+t', encoding='utf-8')
    st.session_state['run'] = {'path': str(root), 'log': log,
                               'process': subprocess.Popen(command, stdout=log, stderr=log)}

@st.fragment(run_every=2)
def show_run():
    """Refresh the selected run's progress, results and recorded agent actions.

    Returns:
        None. Reads the run job from Streamlit session state and displays
        submissions, timings, scores and complete JSONL log lines using the
        module's question lookup. Does nothing when no run is selected.

    Notes:
        Streamlit reruns this fragment every two seconds. The Stop run button
        terminates the runner subprocess; incomplete trailing log lines are
        deferred until the next refresh. Other file or JSON errors propagate.
    """
    job = st.session_state.get('run')
    if not job:
        return
    path = Path(job['path'])
    process = job['process']
    if process.poll() is None:
        st.info('Running selected questions…')
        if st.button('Stop run'):
            process.terminate()
    else:
        st.write('Run finished.' if process.returncode == 0 else f'Run exited with code {process.returncode}.')
        if process.returncode != 0:
            job['log'].seek(0)
            st.code(job['log'].read()[-4000:])
    results = path / 'submission.jsonl'
    if results.exists():
        for row in read_jsonl(results):
            st.subheader(lookup.get(row['id'], {}).get('question', row['id']))
            st.json(row)
            timing_file = path / 'timings.json'
            if timing_file.exists():
                measurements = next((item for item in json.loads(timing_file.read_text(encoding='utf-8'))
                                     if item['id'] == row['id']), None)
                if measurements:
                    st.caption(f"{measurements['seconds']:.2f}s · {measurements['model_calls']} model calls · "
                               f"{measurements['retries']} retries")
    report = path / 'score.json'
    if report.exists():
        st.subheader('Internal score')
        st.json(json.loads(report.read_text()))
    logs = path / 'logs'
    if logs.exists():
        st.subheader('Actions and observations')
        run_questions = path / 'questions.jsonl'
        saved_questions = read_jsonl(run_questions) if run_questions.exists() else []
        for log in sorted(logs.glob('*.jsonl')):
            index = int(log.stem) if log.stem.isdecimal() else -1
            question = saved_questions[index] if 0 <= index < len(saved_questions) else None
            title = f"{question['id']} — {question['question']}" if question else log.stem
            with st.expander(title):
                # The runner flushes one JSON object per line while it is running.
                # Ignore an incomplete trailing line until the next refresh.
                for line in log.read_text(encoding='utf-8').splitlines(keepends=True):
                    if not line.endswith('\n'):
                        break
                    item = json.loads(line)
                    if item.get('stage') == 'code':
                        st.code(item['code'], language='python')
                    elif item.get('stage') == 'memory_step':
                        st.markdown(f"**Agent · step {item['step']}**")
                        if item.get('token_usage'):
                            st.caption(f"Reported model token usage: {item['token_usage']}")
                        if item.get('model_output'):
                            st.text(item['model_output'])
                        if item.get('tool_calls'):
                            st.caption('Tool calls')
                            st.json(item['tool_calls'])
                        if item.get('error'):
                            st.error(str(item['error']))
                        st.caption('Messages in smolagents memory before this step')
                        st.json(item.get('model_input_messages') or [], expanded=False)
                    else:
                        st.text(item.get('text', ''))

show_run()
