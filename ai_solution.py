```python
# backend/src/misthos/store.py:2954
def Store.claim(issue_id):
    issue = Store.get(issue_id)
    return {
        "status": "success",
        "issue": issue,
    }

# backend/src/misthos/store.py:808
def _claim(issue_id, contributor_id):
    issue = Store.get(issue_id)
    issue["claims"] = issue.get("claims", []) + [contributor_id]
    Store.set(issue_id, issue)
    return {"status": "success"}
```