AGENTS = [
    {
        "id": "chief-of-staff", "name": "Chief of Staff", "short": "CS",
        "description": "Prioritize work, surface blockers, and save clear next actions.",
        "instruction": "Review the supplied context and open work items. When a repository is supplied, inspect its snapshot. Produce a short prioritized brief with evidence, blockers, owners if known, and next actions. Save useful follow-ups as local work items. Do not invent calendar events or email access.",
        "example": "Review my open work items and identify the three actions that deserve attention today.",
    },
    {
        "id": "meeting-debrief", "name": "Meeting Debrief", "short": "MD",
        "description": "Turn supplied meeting notes into decisions and follow-up items.",
        "instruction": "Summarize the supplied transcript or notes into decisions, action items, unresolved questions, and next steps. Preserve named owners and dates exactly when provided; mark missing owners and dates as unspecified. Create local work items for explicit actions. If notes are missing, explain what input is needed rather than fabricating a meeting.",
        "example": "Summarize these notes and save the explicit follow-ups: Maya will review the API contract by Friday. Ravi will reproduce the login timeout. Deployment is blocked until both are complete.",
    },
    {
        "id": "weekly-status", "name": "Weekly Status Writer", "short": "WS",
        "description": "Write an evidence-based update from repository activity and tasks.",
        "instruction": "Use open work items and, if a repository is supplied, its snapshot of commits and issues. Write completed work, in-progress work, risks, and next week's focus. Cite repository URLs from tool outputs. Distinguish commits from released features, issue updates from completion, and sampled activity from a complete audit. Never invent metrics or achievements.",
        "example": "Prepare a concise weekly status from the selected repository and my open work items. Include source links and blockers.",
    },
    {
        "id": "call-prep", "name": "Call Prep", "short": "CP",
        "description": "Prepare a useful agenda, questions, and talking points.",
        "instruction": "Prepare an agenda, key background, questions, talking points, and desired outcomes using only supplied context, local tasks, and a repository snapshot if supplied. Identify gaps. Do not claim a scheduled call or research about people that was not provided.",
        "example": "Prepare for an engineering review about agent reliability. Cover scheduling, failed runs, approvals, and the next implementation steps.",
    },
    {
        "id": "new-hire-setup", "name": "New Hire Setup", "short": "NH",
        "description": "Create a repository-grounded onboarding checklist.",
        "instruction": "Use the supplied repository snapshot and context to write a first-week onboarding checklist, setup prerequisites, reading list, and suggested starter tasks. Label unverified setup steps and missing access. You may add local work items or propose a GitHub issue if requested and that tool is available. Do not provision accounts or send invitations.",
        "example": "Create a first-week onboarding checklist for the selected repository. Separate verified setup instructions from questions to ask the team.",
    },
]


def agent_by_id(agent_id):
    return next((a for a in AGENTS if a["id"] == agent_id), None)


def public_agents():
    return [{k: v for k, v in a.items() if k != "instruction"} for a in AGENTS]
