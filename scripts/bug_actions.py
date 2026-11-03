import sys
import os
sys.path.insert(0, os.path.expanduser("~/.gemini/config/scripts"))
from cl_watcher import call_buganizer_mcp_batch

def close_bug_with_fixed_by(issue_id, gerrit_url):
    """
    Closes a crbug issue and fills "Fixed By Code Changes" with the fixing CLs.
    """
    return call_buganizer_mcp_batch([
        ("set_issue_custom_field", {"issueId": str(issue_id), "customFieldId": "1358989",
                                    "customFieldValue": str(gerrit_url)}),
        ("update_issue_status", {"issueId": str(issue_id), "status": "FIXED"}),
        ("get_bugs_with_external", {"query": f"id:{issue_id}", "maxResults": 1, "verbose": True}),
    ])

def reassign_security_merge(issue_id):
    """
    Reassigns bot-filed Chrome [Merge Mxx] requests via safe_add_collaborator and safe_reassign_issue.
    Sequential batch calls as per rules/security_merge_reassign.md.
    """
    # Batch 1 (comment + collaborator)
    batch1 = call_buganizer_mcp_batch([
        ("add_buganizer_comment", {"issueId": str(issue_id), "commentMarkdown": "Adding jlavrova@google.com as collaborator to allow safe reassignment to merges@chromium.org"}),
        ("safe_add_collaborator", {"issueId": str(issue_id), "collaborator": "jlavrova@google.com"}),
    ])
    
    # Batch 2 (reassign)
    batch2 = call_buganizer_mcp_batch([
        ("safe_reassign_issue", {"issueId": str(issue_id), "assignee": "merges@chromium.org"}),
    ])
    
    # Batch 3 (read-back)
    batch3 = call_buganizer_mcp_batch([
        ("get_bugs_with_external", {"query": f"id:{issue_id}", "verbose": True}),
        ("get_user_access", {"issueId": str(issue_id), "relation": "writer"}),
    ])
    
    return [batch1, batch2, batch3]

def render_issue_with_external_auth(issue_id):
    """
    Renders issue with external public content (requires standing authorization).
    """
    res = call_buganizer_mcp_batch([("render_issue_with_external", {"issueId": str(issue_id)})])
    if res:
        return res[0]
    return None

if __name__ == "__main__":
    if len(sys.argv) < 3:
        print("Usage: python3 bug_actions.py <action> <issue_id> [args...]")
        sys.exit(1)
        
    action = sys.argv[1]
    issue_id = sys.argv[2]
    
    if action == "close_fixed":
        gerrit_url = sys.argv[3] if len(sys.argv) > 3 else "NA"
        print(close_bug_with_fixed_by(issue_id, gerrit_url))
    elif action == "reassign_merge":
        print(reassign_security_merge(issue_id))
    elif action == "render":
        print(render_issue_with_external_auth(issue_id))
    else:
        print(f"Unknown action: {action}")
