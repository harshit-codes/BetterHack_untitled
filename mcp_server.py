# mcp_server.py

"""
PromptGit MCP (Master Control Program) Server
-------------------------------------------
A FastAPI-based server that provides Git version control operations through a simplified,
prompt-based interface designed for non-technical users.

Key Features:
- Pending snapshot management (save, list, apply, discard)
- Git commit operations with natural language prompts
- History management and workspace state control
- Error handling with user-friendly messages
"""

import uvicorn
from fastapi import FastAPI, Request, HTTPException
from fastapi.responses import JSONResponse
import os
import json
import subprocess
import datetime

# --- Configuration ---
# Directory structure:
# .mcp_cache/
#   └── pending/
#       ├── {snapshot_id}.patch    # Contains git diff patches
#       └── {snapshot_id}.json     # Contains metadata (prompt, timestamp)
PENDING_SNAPSHOTS_DIR = ".mcp_cache/pending"

# --- FastAPI App Setup ---
app = FastAPI(
    title="PromptGit MCP Server",
    description="MCP Server to manage Git versions based on user prompts, designed for non-technical users.",
)

# --- Helper Functions ---
def ensure_directories():
    """
    Ensure necessary cache directories exist for storing snapshots.
    Creates the PENDING_SNAPSHOTS_DIR if it doesn't exist.
    """
    os.makedirs(PENDING_SNAPSHOTS_DIR, exist_ok=True)

def run_git_command(command: list[str], check: bool = True, text: bool = True):
    """
    Execute Git commands safely with error handling.
    
    Args:
        command (list[str]): Git command as a list of strings (e.g., ["git", "status"])
        check (bool): If True, raises CalledProcessError on non-zero exit codes
        text (bool): If True, returns string output instead of bytes
    
    Returns:
        subprocess.CompletedProcess: Result of the command execution
        
    Raises:
        HTTPException: On various Git-related errors with user-friendly messages
    """
    try:
        result = subprocess.run(command, check=check, capture_output=True, text=text)
        return result
    except FileNotFoundError:
         raise HTTPException(status_code=500, detail="Git command not found. Is Git installed and in PATH?")
    except subprocess.CalledProcessError as e:
        error_message = f"Git command failed: {' '.join(command)}\nError: {e.stderr.strip() or e.stdout.strip()}"
        # Try to provide more context for common errors
        if "not a git repository" in error_message.lower():
            error_message += "\nHint: Is the workspace initialized as a Git repository? ('git init')"
        elif "unknown revision or path not in the working tree" in error_message.lower():
             error_message += "\nHint: Are there any commits in the repository yet?"
        raise HTTPException(status_code=500, detail=error_message)
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"An unexpected error occurred running git: {str(e)}")


# --- MCP Tool Implementations ---

# Tool: save_pending_snapshot [3]
async def tool_save_pending_snapshot(params: dict):
    prompt = params.get("prompt")
    if not prompt:
        raise HTTPException(status_code=400, detail="Missing 'prompt' parameter.")

    snapshot_id = datetime.datetime.now().strftime("%Y%m%d%H%M%S%f") # Added microseconds for uniqueness
    patch_file = os.path.join(PENDING_SNAPSHOTS_DIR, f"{snapshot_id}.patch")
    metadata_file = os.path.join(PENDING_SNAPSHOTS_DIR, f"{snapshot_id}.json")

    # Generate patch of current changes (staged and unstaged)
    # Use 'git diff HEAD' to compare against the last commit
    diff_result = run_git_command(["git", "diff", "HEAD"], check=False) # Don't fail if no diff

    patch = diff_result.stdout
    if not patch.strip():
         # Check for untracked files as changes too
         status_result = run_git_command(["git", "status", "--porcelain"])
         if not status_result.stdout.strip():
              return {"status": "no_changes", "message": "No changes (including untracked files) detected relative to the last commit."}
         else:
             # If only untracked files, we still want to save a snapshot 'idea'
             # but the patch will be empty. We proceed, but the user experience
             # on applying might be weird. Consider adding untracked files to the 'patch' concept?
             # For now, we save an empty patch with metadata.
             pass


    # Save patch to file
    with open(patch_file, "w") as f:
        f.write(patch)

    # Save metadata with prompt
    metadata = {
        "id": snapshot_id,
        "prompt": prompt,
        "timestamp": datetime.datetime.now().isoformat()
    }
    with open(metadata_file, "w") as f:
        json.dump(metadata, f)

    # Optionally reset workspace to last commit (Make this configurable?)
    # For now, we follow the spec and reset.
    run_git_command(["git", "reset", "--hard", "HEAD"])
    # Consider git clean -fd for untracked files? Needs user confirmation.

    return {"status": "success", "snapshot_id": snapshot_id, "message": f"Pending snapshot '{prompt}' saved. Workspace reset."}

# Tool: list_pending_snapshots [4]
async def tool_list_pending_snapshots(params: dict):
    snapshots = []
    ensure_directories() # Ensure dir exists before listing
    for filename in os.listdir(PENDING_SNAPSHOTS_DIR):
        if filename.endswith('.json'):
            metadata_file = os.path.join(PENDING_SNAPSHOTS_DIR, filename)
            try:
                with open(metadata_file, 'r') as f:
                    metadata = json.load(f)
                    # Check if corresponding patch file exists
                    patch_file = os.path.join(PENDING_SNAPSHOTS_DIR, f"{metadata.get('id', '')}.patch")
                    if os.path.exists(patch_file):
                         snapshots.append(metadata)
                    else:
                         # Metadata exists but patch is missing, log or handle inconsistency
                         print(f"Warning: Metadata found for {metadata.get('id')} but patch file is missing.")
            except json.JSONDecodeError:
                 print(f"Warning: Could not decode JSON from {metadata_file}")
            except Exception as e:
                 print(f"Error reading snapshot metadata {metadata_file}: {e}")

    # Sort by timestamp descending
    snapshots.sort(key=lambda x: x.get('timestamp', ''), reverse=True)
    return {"status": "success", "snapshots": snapshots}

# Tool: apply_pending_snapshot [5]
async def tool_apply_pending_snapshot(params: dict):
    snapshot_id = params.get("snapshot_id")
    if not snapshot_id:
        raise HTTPException(status_code=400, detail="Missing 'snapshot_id' parameter.")

    patch_file = os.path.join(PENDING_SNAPSHOTS_DIR, f"{snapshot_id}.patch")
    metadata_file = os.path.join(PENDING_SNAPSHOTS_DIR, f"{snapshot_id}.json")

    if not os.path.exists(patch_file) or not os.path.exists(metadata_file):
        raise HTTPException(status_code=404, detail=f"Snapshot '{snapshot_id}' not found.")

    # Reset workspace to last commit before applying patch
    run_git_command(["git", "reset", "--hard", "HEAD"])
    # Consider git clean -fd for untracked files? Needs user confirmation.

    # Apply patch
    # Use --reject to handle potential conflicts gracefully instead of failing hard
    apply_result = run_git_command(["git", "apply", "--reject", patch_file], check=False)

    if apply_result.returncode != 0:
        # Git apply failed, likely conflicts
        conflicts_exist = os.path.exists(".git/rebase-apply") # Check if git created conflict markers
        message = f"Failed to apply snapshot {snapshot_id}. Conflicts likely occurred. Please resolve manually or discard changes."
        # Attempt to provide more info if possible
        if ".rej" in apply_result.stderr or ".rej" in apply_result.stdout:
            message += " Conflict marker files (.rej) may have been created."
        return {"status": "error", "message": message, "details": apply_result.stderr or apply_result.stdout}


    # Get prompt from metadata for success message
    prompt = "Unknown Prompt"
    try:
        with open(metadata_file, 'r') as f:
            metadata = json.load(f)
            prompt = metadata.get('prompt', prompt)
    except Exception:
        pass # Ignore errors reading metadata for the message

    return {"status": "success", "message": f"Applied snapshot '{prompt}' ({snapshot_id})."}

# Tool: discard_pending_snapshot (New based on discussion)
async def tool_discard_pending_snapshot(params: dict):
    snapshot_id = params.get("snapshot_id")
    if not snapshot_id:
        raise HTTPException(status_code=400, detail="Missing 'snapshot_id' parameter.")

    patch_file = os.path.join(PENDING_SNAPSHOTS_DIR, f"{snapshot_id}.patch")
    metadata_file = os.path.join(PENDING_SNAPSHOTS_DIR, f"{snapshot_id}.json")

    deleted_patch = False
    deleted_meta = False

    if os.path.exists(patch_file):
        try:
            os.remove(patch_file)
            deleted_patch = True
        except OSError as e:
            raise HTTPException(status_code=500, detail=f"Failed to delete patch file {patch_file}: {e}")

    if os.path.exists(metadata_file):
        try:
            os.remove(metadata_file)
            deleted_meta = True
        except OSError as e:
             # If patch was deleted but meta fails, we are in inconsistent state, but report failure
            raise HTTPException(status_code=500, detail=f"Failed to delete metadata file {metadata_file}: {e}")

    if not deleted_patch and not deleted_meta:
         raise HTTPException(status_code=404, detail=f"Snapshot '{snapshot_id}' not found.")

    return {"status": "success", "message": f"Pending snapshot {snapshot_id} discarded."}


# Tool: commit_current_changes [6]
async def tool_commit_current_changes(params: dict):
    prompt = params.get("prompt")
    if not prompt:
        raise HTTPException(status_code=400, detail="Missing 'prompt' parameter.")

    # Check if there's anything to commit (staged or unstaged)
    status_result = run_git_command(["git", "status", "--porcelain"])
    if not status_result.stdout.strip():
        return {"status": "no_changes", "message": "No changes to commit."}

    # Stage all changes (including untracked files)
    run_git_command(["git", "add", "."])

    commit_message = f"MCP Committed Prompt: {prompt}"
    # Use --allow-empty-message? No, prompt is required.
    # Use --no-verify to bypass pre-commit hooks if needed? Maybe add as option.
    run_git_command(["git", "commit", "-m", commit_message])

    return {"status": "success", "message": f"Changes committed with prompt: '{prompt}'."}

# Tool: list_history [7]
async def tool_list_history(params: dict):
    try:
        # Use a separator unlikely to appear in messages/names: |~|
        log_format = "%H|~|%an|~|%ad|~|%s"
        log_result = run_git_command([
            "git", "log", f"--pretty=format:{log_format}", "--date=iso"
        ])
        log_output = log_result.stdout
    except HTTPException as e:
         # Handle case where there are no commits yet
         if "does not have any commits yet" in e.detail:
             return {"status": "success", "commits": []}
         raise e # Re-raise other git errors

    commits = []
    for line in log_output.strip().splitlines():
        parts = line.split('|~|', 3)
        if len(parts) < 4:
            print(f"Warning: Could not parse log line: {line}")
            continue
        commit_hash, author, date, message = parts
        prompt = None
        prefix = "MCP Committed Prompt:"
        if message.startswith(prefix):
            prompt = message[len(prefix):].strip()

        commits.append({
            "commit_hash": commit_hash,
            "author": author,
            "date": date,
            "prompt": prompt, # Will be None if not committed via MCP
            "message": message # Full original message
        })
    return {"status": "success", "commits": commits}

# Tool: revert_to_committed_state [8]
async def tool_revert_to_committed_state(params: dict):
    commit_id = params.get("commit_id")
    if not commit_id:
        raise HTTPException(status_code=400, detail="Missing 'commit_id' parameter.")

    # Use checkout. Be careful, this discards local changes.
    # Consider adding a check for uncommitted changes first?
    run_git_command(["git", "checkout", commit_id])

    return {"status": "success", "message": f"Workspace reverted to commit {commit_id}."}

# Tool: discard_current_workspace_changes [9]
async def tool_discard_current_workspace_changes(params: dict):
    # Confirmation should ideally happen client-side before calling this.
    delete_untracked = params.get("delete_untracked", False)

    # Reset tracked files
    run_git_command(["git", "reset", "--hard", "HEAD"])
    message = "Workspace reset to last commit (tracked files)."

    if delete_untracked:
        # Clean untracked files and directories
        run_git_command(["git", "clean", "-fd"])
        message += " Untracked files and directories also removed."

    return {"status": "success", "message": message}


# --- Main Tool Dispatcher ---
@app.post("/tools/{tool_name}")
async def run_tool_endpoint(tool_name: str, request: Request):
    """Endpoint to run MCP tools."""
    try:
        payload = await request.json()
        params = payload.get("params", {})
    except json.JSONDecodeError:
        raise HTTPException(status_code=400, detail="Invalid JSON payload.")

    tool_functions = {
        "save_pending_snapshot": tool_save_pending_snapshot,
        "list_pending_snapshots": tool_list_pending_snapshots,
        "apply_pending_snapshot": tool_apply_pending_snapshot,
        "discard_pending_snapshot": tool_discard_pending_snapshot,
        "commit_current_changes": tool_commit_current_changes,
        "list_history": tool_list_history,
        "revert_to_committed_state": tool_revert_to_committed_state,
        "discard_current_workspace_changes": tool_discard_current_workspace_changes,
    }

    if tool_name not in tool_functions:
        raise HTTPException(status_code=404, detail=f"Tool '{tool_name}' not found.")

    try:
        # Ensure directories exist before running any tool
        ensure_directories()
        # Run the selected tool function
        result = await tool_functions[tool_name](params)
        return JSONResponse(content=result)
    except HTTPException as e:
        # Re-raise HTTPExceptions directly
        raise e
    except Exception as e:
        # Catch unexpected errors during tool execution
        print(f"Error executing tool '{tool_name}': {e}") # Log detailed error server-side
        raise HTTPException(status_code=500, detail=f"Internal server error executing tool '{tool_name}': {str(e)}")


# --- Server Health Check and Run ---
@app.get("/health")
def health_check():
    """Basic health check endpoint."""
    return {"status": "ok"}

if __name__ == "__main__":
    print("Starting PromptGit MCP Server...")
    print(f"Pending snapshots will be stored in: {os.path.abspath(PENDING_SNAPSHOTS_DIR)}")
    # Ensure the workspace is a git repo on startup? Or handle errors per command.
    # run_git_command(["git", "rev-parse", "--is-inside-work-tree"]) # Example check
    print("Starting server on http://127.0.0.1:3000")
    uvicorn.run(app, host="127.0.0.1", port=3000) # Use --reload for development: uvicorn mcp_server:app --reload
