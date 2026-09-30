# Code for Outskill GenAI Workshops

## .env
Prepare the following `.env` file in your root folder:
```
OPENAI_API_KEY=
OPENROUTER_API_KEY=
EXA_API_KEY=
ADZUNA_APP_ID=
ADZUNA_API_KEY=
```

## Automatic work assistants

[Work Agents task hub](task_hub/README.md) is a self-contained application with five work assistants, automatic background execution, daily/weekly schedules, persistent results, a browser dashboard, and approval-controlled GitHub issue proposals.

Try the explicitly labeled offline demonstration from the repository root:

```bash
python -m task_hub --demo
```

Open `http://127.0.0.1:8765`. See the task hub README for Windows timezone setup, live Ollama configuration, GitHub permissions, and Docker startup.
