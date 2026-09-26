"""The labels the bot uses: name -> (color, description). No dependencies, so setup scripts can import it
without installing the app's libraries."""

LABEL_COLORS = {
    "devin:proposed": ("d4c5f9", "Scanner filed it; awaiting a human to approve"),
    "devin:ready": ("0e8a16", "Approved: Devin will work on this"),
    "devin:in-progress": ("1d76db", "A Devin session is working on this"),
    "devin:blocked": ("d93f0b", "Devin needs a human answer"),
    "devin:in-review": ("fbca04", "Pull request open and verified; awaiting review"),
    "devin:failed": ("b60205", "Devin could not complete this"),
    "devin:rejected": ("6a737d", "A human decided not to do this"),
    "devin:knowledge-base-improvement": ("c2e0c6", "Proposed rule changes for knowledge/"),
    "type:dependency": ("ededed", ""),
    "type:lint": ("ededed", ""),
    "type:tests": ("ededed", ""),
    "type:bug": ("ededed", ""),
    "type:other": ("ededed", ""),
}
