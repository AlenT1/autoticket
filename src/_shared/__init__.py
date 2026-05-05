"""Shared infrastructure for file_to_jira and jira_task_agent.

Boundaries unified at merge time (stage 2):
- llm/   — LLMProvider ABC + OpenAICompatProvider + AnthropicProvider
- io/    — sources/ (input) and sinks/ (output) — added in steps 8 and 9
"""
