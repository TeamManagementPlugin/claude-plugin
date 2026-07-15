# Research Conclusion Sub-Protocol

## Purpose

Present research findings to the user for review, get explicit confirmation, and complete the research protocol. This is the final quality gate before archiving.

## 1. Present Summary

Provide the user with a concise summary of:
- **Research question**: What was investigated
- **Key findings**: The most important discoveries (3-5 bullet points)
- **Recommendation**: Your conclusion and suggested next steps
- **Follow-up tasks**: Any implementation work that should be created as new tasks

Keep the summary brief — the full findings are in the task file. The summary should give the user enough to decide if the research is complete.

## 2. User Review

The user reviews the findings. During this phase:
- Answer questions about the research
- Clarify any findings or recommendations
- If the user wants deeper investigation: use `protocol_goto(step_name="exploration")` to go back
- If the user wants to refine the written document: use `protocol_goto(step_name="synthesis")` to go back

**CRITICAL**: Do NOT call `protocol_advance` until the user explicitly confirms the research is complete.

## 3. User Confirmation

Wait for explicit confirmation. Acceptable confirmations:
- "Looks good", "approved", "research complete", "LGTM", "done"
- Any clear positive confirmation

**NOT acceptable**:
- Silence or no response
- "I'll review later" (wait for actual review)
- Ambiguous responses (ask for clarification)

## 4. Follow-Up Task Suggestions

Before completing, ask the user:
- Should any follow-up implementation tasks be created?
- Should a `task` protocol be started for the recommended approach?

This is optional — the user may want to create follow-up tasks in a separate session.

## 5. Automated Completion

When the user confirms and you call `protocol_advance`, the engine automatically:

1. Archives the research task file to team-management/tasks/done/ with an optional lightweight commit
2. Updates the provider issue to completed/closed (if issue tracking is enabled)
3. Cleans up task-scoped state files
4. Resets current task state

**Note**: If archival fails (e.g., git commit issue), cleanup still proceeds. The file move is the critical part — the optional commit is a convenience.

## Going Back (protocol_goto)

If the user wants changes:

```
User: "I think we need to investigate option C as well"
AI: Let me go back to exploration to investigate that.
    → calls protocol_goto(step_name="exploration", reason="User wants option C investigated")
```

```
User: "The comparison matrix needs more detail"
AI: Let me go back to synthesis to expand the analysis.
    → calls protocol_goto(step_name="synthesis", reason="User wants more detailed comparison matrix")
```

## Important Notes

- NEVER call protocol_advance without explicit user confirmation
- Research produces knowledge artifacts, not code — there is no commit/push/MR step
- The optional lightweight commit during archival captures the research document in git history
- If the research should be abandoned, use `protocol_abort` instead of `protocol_advance`
