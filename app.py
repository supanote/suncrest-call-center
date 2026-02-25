from flask import Flask, render_template, jsonify, request
import requests
import os
import psycopg2
from psycopg2.extras import RealDictCursor
from dotenv import load_dotenv
from datetime import datetime

load_dotenv()

app = Flask(__name__)

API_KEY = os.getenv("VAPI_PRIVATE_KEY")
ASSISTANT_ID = os.getenv("ASSISTANT_ID")
PHONE_NUMBER_ID = os.getenv("VAPI_PHONE_NUMBER_ID")
BASE_URL = "https://api.vapi.ai/call"

HEADERS = {
    "Authorization": f"Bearer {API_KEY}",
    "Content-Type": "application/json",
}

DB_CONFIG = {
    "dbname": os.getenv("RAILWAY_DB_NAME", "railway"),
    "user": os.getenv("RAILWAY_DB_USER", "postgres"),
    "password": os.getenv("RAILWAY_DB_PASSWORD"),
    "host": os.getenv("RAILWAY_DB_HOST"),
    "port": os.getenv("RAILWAY_DB_PORT"),
}


def get_db_connection():
    return psycopg2.connect(**DB_CONFIG, cursor_factory=RealDictCursor)


def init_db():
    """Initialize database tables"""
    conn = get_db_connection()
    cur = conn.cursor()
    cur.execute("""
        CREATE TABLE IF NOT EXISTS deleted_calls (
            call_id VARCHAR(255) PRIMARY KEY,
            deleted_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
        )
    """)
    conn.commit()
    cur.close()
    conn.close()


def get_deleted_call_ids():
    """Get set of deleted call IDs"""
    try:
        conn = get_db_connection()
        cur = conn.cursor()
        cur.execute("SELECT call_id FROM deleted_calls")
        rows = cur.fetchall()
        cur.close()
        conn.close()
        return {row["call_id"] for row in rows}
    except Exception:
        return set()


def get_all_feedback():
    """Get all feedback ratings as a dict {call_id: rating}"""
    try:
        conn = get_db_connection()
        cur = conn.cursor()
        cur.execute("SELECT call_id, rating FROM call_feedback")
        rows = cur.fetchall()
        cur.close()
        conn.close()
        return {row["call_id"]: row["rating"] for row in rows}
    except Exception:
        return {}


def extract_transcript(call):
    """Extract and format transcript from call data as structured list"""
    messages_list = []
    
    # VAPI stores conversation in artifact.messages (not top-level messages)
    artifact = call.get("artifact", {})
    
    # Try artifact.messages first (primary location for conversation)
    if artifact:
        artifact_messages = artifact.get("messages") or []
        for msg in artifact_messages:
            role = msg.get("role", "")
            content = msg.get("message", "") or msg.get("content", "") or msg.get("text", "")
            
            # Skip system messages and tool calls
            if not content or role in ["system", "tool_call", "tool_call_result", "tool-call", "tool-call-result"]:
                continue
            
            if role in ["bot", "assistant"]:
                messages_list.append({"role": "bot", "content": content})
            elif role in ["user", "customer"]:
                messages_list.append({"role": "user", "content": content})
        
        # If no structured messages, try artifact.transcript (plain text)
        if not messages_list and artifact.get("transcript"):
            return parse_plain_transcript(artifact.get("transcript"))
    
    # Fallback: try top-level messages (older VAPI format)
    if not messages_list:
        messages = call.get("messages") or []
        for msg in messages:
            role = msg.get("role", "")
            content = msg.get("message", "") or msg.get("content", "") or msg.get("text", "")
            
            if not content or role in ["system", "tool_call", "tool_call_result"]:
                continue
            
            if role in ["bot", "assistant"]:
                messages_list.append({"role": "bot", "content": content})
            elif role in ["user", "customer"]:
                messages_list.append({"role": "user", "content": content})
    
    # Fallback: try direct transcript field
    if not messages_list:
        plain_transcript = call.get("transcript")
        if plain_transcript and isinstance(plain_transcript, str):
            return parse_plain_transcript(plain_transcript)
    
    # No transcript found - provide helpful message
    if not messages_list:
        ended_reason = call.get("endedReason", "")
        if "error" in ended_reason.lower() or "failed" in ended_reason.lower():
            return [{"role": "system", "content": f"Call ended with error: {ended_reason}"}]
        elif call.get("stereoRecordingUrl") or call.get("recordingUrl") or artifact.get("recordingUrl"):
            return [{"role": "system", "content": "Recording available but transcript not yet processed"}]
        return []
    
    return messages_list


def parse_plain_transcript(text):
    """Parse a plain text transcript into structured messages"""
    messages_list = []
    lines = text.strip().split('\n')
    
    for line in lines:
        line = line.strip()
        if not line:
            continue
        
        # Check for common speaker prefixes
        if line.startswith("AI:") or line.startswith("Anna:") or line.startswith("Bot:") or line.startswith("Assistant:"):
            content = line.split(":", 1)[1].strip() if ":" in line else line
            messages_list.append({"role": "bot", "content": content})
        elif line.startswith("User:") or line.startswith("Customer:") or line.startswith("Human:"):
            content = line.split(":", 1)[1].strip() if ":" in line else line
            messages_list.append({"role": "user", "content": content})
        else:
            # If no prefix, treat as continuation or unknown
            if messages_list:
                messages_list[-1]["content"] += " " + line
            else:
                messages_list.append({"role": "user", "content": line})
    
    return messages_list


def format_call(call):
    """Format a call object for the frontend"""
    created_at = call.get("createdAt", "")
    time_str = ""
    if created_at:
        try:
            dt = datetime.fromisoformat(created_at.replace('Z', '+00:00'))
            time_str = dt.strftime("%b %d, %Y %I:%M %p")
        except:
            time_str = created_at[:16]
    
    # Try to get duration from multiple sources
    duration = call.get("duration") or call.get("durationSeconds") or 0
    
    # If no direct duration, calculate from startedAt and endedAt
    if not duration:
        started_at = call.get("startedAt")
        ended_at = call.get("endedAt")
        if started_at and ended_at:
            try:
                start_dt = datetime.fromisoformat(started_at.replace('Z', '+00:00'))
                end_dt = datetime.fromisoformat(ended_at.replace('Z', '+00:00'))
                duration = (end_dt - start_dt).total_seconds()
            except:
                pass
    
    if duration:
        mins = int(duration) // 60
        secs = int(duration) % 60
        duration_str = f"{mins}m {secs}s" if mins > 0 else f"{secs}s"
    else:
        duration_str = "—"
    
    cost = call.get("cost", 0)
    cost_str = f"${cost:.3f}" if cost else "—"
    
    call_type = call.get("type", "outboundPhoneCall")
    if "web" in call_type.lower():
        type_display = "Web"
        type_icon = "globe"
    elif "inbound" in call_type.lower():
        type_display = "Inbound"
        type_icon = "phone-incoming"
    else:
        type_display = "Outbound"
        type_icon = "phone-outgoing"
    
    # Determine status - check for failed conditions
    status = call.get("status", "unknown")
    ended_reason = call.get("endedReason", "")
    
    # Mark as failed if ended reason indicates failure
    failed_reasons = [
        "assistant-error", "assistant-not-found", "db-error", "no-server-available",
        "license-check-failed", "pipeline-error", "silence-timed-out", "voicemail",
        "assistant-request-returned-error", "assistant-request-returned-invalid-assistant",
        "phone-call-provider-closed-websocket", "assistant-ended-call-with-error",
        "customer-did-not-answer", "assistant-said-end-call-phrase", "exceeded-max-duration",
        "manually-canceled", "phone-call-provider-bypass-enabled-but-no-call-received"
    ]
    
    if ended_reason.lower() in [r.lower() for r in failed_reasons]:
        status = "failed"
    elif ended_reason.lower() == "assistant-forwarded-call":
        status = "ended"  # Forwarded calls are not failures
    # Also check for error in endedReason string
    elif "error" in ended_reason.lower() or "failed" in ended_reason.lower():
        status = "failed"
    
    return {
        "id": call.get("id", ""),
        "short_id": call.get("id", "")[:12] + "...",
        "customer": call.get("customer", {}).get("number", "—"),
        "type": type_display,
        "type_icon": type_icon,
        "status": status,
        "ended_reason": ended_reason or "—",
        "date_time": time_str,
        "duration": duration_str,
        "cost": cost_str,
        "transcript": extract_transcript(call),  # Now returns list of {role, content}
        "recording_url": call.get("stereoRecordingUrl") or call.get("recordingUrl") or "",
    }


@app.route("/")
def index():
    return render_template("index.html")


@app.route("/api/calls")
def get_calls():
    try:
        r = requests.get(f"{BASE_URL}?assistantId={ASSISTANT_ID}", headers=HEADERS, timeout=10)
        if r.status_code // 100 == 2:
            data = r.json()
            if isinstance(data, list):
                calls = data
            else:
                calls = data.get("calls", data.get("data", []))
            
            deleted_ids = get_deleted_call_ids()
            filtered_calls = [c for c in calls if c.get("id") not in deleted_ids]
            
            feedback_map = get_all_feedback()
            formatted_calls = []
            for c in filtered_calls:
                call_data = format_call(c)
                call_data["rating"] = feedback_map.get(c.get("id"))
                formatted_calls.append(call_data)
            
            return jsonify(formatted_calls)
        return jsonify({"error": f"API error: {r.status_code}"}), 500
    except Exception as e:
        return jsonify({"error": str(e)}), 500


@app.route("/api/calls/<call_id>")
def get_call(call_id):
    try:
        deleted_ids = get_deleted_call_ids()
        if call_id in deleted_ids:
            return jsonify({"error": "Call not found"}), 404
        
        r = requests.get(f"{BASE_URL}/{call_id}", headers=HEADERS, timeout=10)
        if r.status_code // 100 == 2:
            return jsonify(format_call(r.json()))
        return jsonify({"error": "Call not found"}), 404
    except Exception as e:
        return jsonify({"error": str(e)}), 500


@app.route("/api/calls/<call_id>/raw")
def get_call_raw(call_id):
    """Debug endpoint to see raw VAPI response"""
    try:
        r = requests.get(f"{BASE_URL}/{call_id}", headers=HEADERS, timeout=10)
        if r.status_code // 100 == 2:
            return jsonify(r.json())
        return jsonify({"error": "Call not found"}), 404
    except Exception as e:
        return jsonify({"error": str(e)}), 500


@app.route("/api/calls", methods=["POST"])
def create_call():
    data = request.json
    phone = data.get("phone")
    if not phone:
        return jsonify({"error": "Phone number required"}), 400
    
    payload = {
        "assistantId": ASSISTANT_ID,
        "phoneNumberId": PHONE_NUMBER_ID,
        "customer": {"number": phone}
    }
    
    try:
        r = requests.post(BASE_URL, headers=HEADERS, json=payload, timeout=10)
        if r.status_code // 100 == 2:
            return jsonify(format_call(r.json()))
        return jsonify({"error": r.text}), 500
    except Exception as e:
        return jsonify({"error": str(e)}), 500


@app.route("/api/calls/<call_id>", methods=["DELETE"])
def delete_call(call_id):
    """Soft delete - store call ID in deleted_calls table to hide from UI"""
    try:
        conn = get_db_connection()
        cur = conn.cursor()
        cur.execute(
            "INSERT INTO deleted_calls (call_id) VALUES (%s) ON CONFLICT (call_id) DO NOTHING",
            (call_id,)
        )
        conn.commit()
        cur.close()
        conn.close()
        return jsonify({"success": True})
    except Exception as e:
        return jsonify({"error": str(e)}), 500


@app.route("/api/feedback/<call_id>", methods=["GET"])
def get_feedback(call_id):
    try:
        conn = get_db_connection()
        cur = conn.cursor()
        cur.execute(
            "SELECT call_id, rating, comment, created_at FROM call_feedback WHERE call_id = %s",
            (call_id,)
        )
        row = cur.fetchone()
        cur.close()
        conn.close()
        
        if row:
            return jsonify({
                "call_id": row["call_id"],
                "rating": row["rating"],
                "comment": row["comment"],
                "created_at": row["created_at"].isoformat() if row["created_at"] else None
            })
        return jsonify({"call_id": call_id, "rating": None, "comment": None})
    except Exception as e:
        return jsonify({"error": str(e)}), 500


@app.route("/api/feedback/<call_id>", methods=["POST"])
def save_feedback(call_id):
    data = request.json
    rating = data.get("rating")
    comment = data.get("comment", "")
    
    if not rating or not (1 <= rating <= 5):
        return jsonify({"error": "Rating must be between 1 and 5"}), 400
    
    try:
        conn = get_db_connection()
        cur = conn.cursor()
        cur.execute("""
            INSERT INTO call_feedback (call_id, rating, comment)
            VALUES (%s, %s, %s)
            ON CONFLICT (call_id) 
            DO UPDATE SET rating = EXCLUDED.rating, comment = EXCLUDED.comment, updated_at = CURRENT_TIMESTAMP
            RETURNING call_id, rating, comment
        """, (call_id, rating, comment))
        row = cur.fetchone()
        conn.commit()
        cur.close()
        conn.close()
        
        return jsonify({
            "success": True,
            "call_id": row["call_id"],
            "rating": row["rating"],
            "comment": row["comment"]
        })
    except Exception as e:
        return jsonify({"error": str(e)}), 500


init_db()

if __name__ == "__main__":
    app.run(debug=True, port=5001)
