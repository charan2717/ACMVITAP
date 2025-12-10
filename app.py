# app.py (complete — drop-in replacement)
from flask import (
    Flask, render_template, request, redirect, url_for, send_file,
    session, Response, jsonify, flash, abort
)
from functools import wraps
from pymongo import MongoClient, errors as pymongo_errors
from bson.objectid import ObjectId
from dotenv import load_dotenv
from datetime import datetime
from io import BytesIO
import pandas as pd
import csv
import os

# load env
load_dotenv()

app = Flask(__name__)
app.secret_key = os.getenv("SECRET_KEY", "fallback_secret")

# Mongo config
MONGO_URI = os.getenv("MONGO_URI", "mongodb://localhost:27017")
MONGO_DBNAME = os.getenv("MONGO_DBNAME", "ACM")

client = MongoClient(MONGO_URI)
db = client[MONGO_DBNAME]

# Collections
teams_collection = db.get_collection("registrations")            # registrations (was teams)
events_collection = db.get_collection("events")                 # event definitions
legacy_collection = db.get_collection("hackathon_workshop")     # preserve original if used

# Utility: ensure indexes
def init_db():
    try:
        teams_collection.create_index("event_id")
    except Exception:
        pass
    try:
        events_collection.create_index("event_name", unique=True)
    except Exception:
        pass
    try:
        legacy_collection.create_index("team_lead_email", unique=False)
    except Exception:
        pass

# Helper: convert BSON docs to JSON-serializable dicts
def doc_to_json(doc):
    if not doc:
        return doc
    doc = dict(doc)
    if "_id" in doc:
        try:
            doc["_id"] = str(doc["_id"])
        except Exception:
            doc["_id"] = doc.get("_id")
    for k, v in doc.items():
        if isinstance(v, datetime):
            doc[k] = v.isoformat()
    return doc

# Admin guard
def admin_required(f):
    @wraps(f)
    def wrapped(*args, **kwargs):
        if 'admin' not in session:
            return redirect(url_for('admin_login'))
        return f(*args, **kwargs)
    return wrapped

# -------------------------
# Public routes / pages
# -------------------------
@app.route('/')
def home():
    return render_template('home.html')

@app.route('/treasure')
def treasure():
    return render_template('treasure.html')

@app.route('/upcoming_events')
def upcoming_events():
    return render_template('upcoming_events.html')

# Keep original /team_register route for backward compatibility:
# Redirect user to choose_event so they pick an event first.
@app.route('/team_register', methods=['GET', 'POST'])
def team_register_root():
    # If someone POSTs to legacy endpoint, fallback to choose_event page.
    # Safer to redirect user to choose_event to pick the relevant event.
    return redirect(url_for('choose_event'))

# Choose event page (list active events)
@app.route('/choose_event')
def choose_event():
    try:
        events = [doc_to_json(e) for e in events_collection.find({"active": True}).sort("created_at", -1)]
    except Exception:
        events = []
    return render_template('choose_event.html', events=events)

# Dynamic registration for a specific event
# Combined route: supports both /team_register (no arg) and /team_register/<event_id>
@app.route('/team_register/', defaults={'event_id': None}, methods=['GET', 'POST'])
@app.route('/team_register/<event_id>', methods=['GET', 'POST'])
def team_register(event_id):
    """
    If event_id is None -> redirect to choose_event to pick one.
    Otherwise, act as the dynamic registration handler for the event.
    """
    # If no event specified, show the chooser
    if not event_id:
        # If someone POSTs to legacy /team_register without event, redirect to chooser.
        if request.method == 'POST':
            return redirect(url_for('choose_event'))
        return redirect(url_for('choose_event'))

    # Try to find event document
    try:
        event_doc = events_collection.find_one({"_id": ObjectId(event_id)})
    except Exception:
        event_doc = None

    if not event_doc:
        return "Invalid event", 404

    event = doc_to_json(event_doc)

    if request.method == 'POST':
        # apply event rules
        require_team_name = event.get("require_team_name", False)
        min_members = int(event.get("min_members", 1))
        max_members = int(event.get("max_members", 1))

        # gather members
        members = []
        missing_required = None
        for i in range(1, max_members + 1):
            name = request.form.get(f"member_{i}_name", "").strip()
            email = request.form.get(f"member_{i}_email", "").strip()
            reg_no = request.form.get(f"member_{i}_reg_no", "").strip()

            if i <= min_members:
                if not name or not email:
                    missing_required = f"Member {i} name and email are required."
                    break

            if name or email or reg_no or i <= min_members:
                members.append({"name": name, "email": email, "reg_no": reg_no})

        if missing_required:
            return render_template('team_register.html', event=event, error=missing_required, form=request.form), 400

        team_lead_name = request.form.get("team_lead_name", "").strip()
        team_lead_email = request.form.get("team_lead_email", "").strip()
        if not team_lead_name or not team_lead_email:
            return render_template('team_register.html', event=event, error="Team lead name and email are required.", form=request.form), 400

        team_name_field = request.form.get("team_name", "").strip() if require_team_name else None
        if require_team_name and not team_name_field:
            return render_template('team_register.html', event=event, error="Team name is required for this event.", form=request.form), 400

        reg_doc = {
            "event_id": event["_id"],
            "event_name": event["event_name"],
            "team_name": team_name_field,
            "team_lead_name": team_lead_name,
            "team_lead_email": team_lead_email,
            "team_lead_phone": request.form.get("team_lead_phone", "").strip(),
            "team_lead_reg_no": request.form.get("team_lead_reg_no", "").strip(),
            "members": members,
            "created_at": datetime.utcnow()
        }

        try:
            res = teams_collection.insert_one(reg_doc)
            inserted = teams_collection.find_one({"_id": res.inserted_id})
            return render_template('download_info.html', data=doc_to_json(inserted))
        except Exception as e:
            app.logger.exception("Error inserting registration")
            return render_template('team_register.html', event=event, error=f"An error occurred: {e}", form=request.form), 500

    # GET request: render dynamic form
    return render_template('team_register.html', event=event)


# -------------------------
# Admin authentication + dashboard
# -------------------------
@app.route('/admin_login', methods=['GET', 'POST'])
def admin_login():
    if request.method == 'POST':
        username = request.form.get('username', '')
        password = request.form.get('password', '')

        ADMIN_USER = os.getenv("ADMIN_USER", "admin")
        ADMIN_PASS = os.getenv("ADMIN_PASS", "acmvitap")

        if username == ADMIN_USER and password == ADMIN_PASS:
            session['admin'] = True
            return redirect(url_for('admin_dashboard'))
        return render_template('admin_login.html', error="Invalid credentials. Try again.")
    return render_template('admin_login.html')

@app.route('/admin_dashboard')
@admin_required
def admin_dashboard():
    return render_template('admin_dashboard.html')

@app.route('/logout')
def logout():
    session.pop('admin', None)
    return redirect(url_for('home'))

# -------------------------
# Admin: Events management (create, edit, delete)
# -------------------------
@app.route('/admin/events', methods=['GET', 'POST'])
@admin_required
def admin_events():
    if request.method == 'POST':
        event_name = request.form.get('event_name', '').strip()
        require_team_name = request.form.get('require_team_name') == 'on'
        try:
            min_members = max(1, int(request.form.get('min_members', 1)))
        except ValueError:
            min_members = 1
        try:
            max_members = max(min_members, int(request.form.get('max_members', min_members)))
        except ValueError:
            max_members = min_members

        doc = {
            "event_name": event_name,
            "require_team_name": require_team_name,
            "min_members": min_members,
            "max_members": max_members,
            "active": True,
            "created_at": datetime.utcnow()
        }
        try:
            events_collection.insert_one(doc)
            flash("Event created.", "success")
        except pymongo_errors.DuplicateKeyError:
            flash("Event name already exists.", "error")
        except Exception as e:
            app.logger.exception("Error creating event")
            flash(f"Error: {e}", "error")
        return redirect(url_for('admin_events'))

    events = [doc_to_json(e) for e in events_collection.find().sort("created_at", -1)]
    return render_template('admin_events.html', events=events)

@app.route('/admin/event/<event_id>/edit', methods=['GET', 'POST'])
@admin_required
def admin_edit_event(event_id):
    try:
        oid = ObjectId(event_id)
    except Exception:
        flash("Invalid event id.", "error")
        return redirect(url_for('admin_events'))

    event = events_collection.find_one({"_id": oid})
    if not event:
        flash("Event not found.", "error")
        return redirect(url_for('admin_events'))

    if request.method == 'POST':
        event_name = request.form.get('event_name', '').strip()
        require_team_name = request.form.get('require_team_name') == 'on'
        try:
            min_members = max(1, int(request.form.get('min_members', 1)))
        except ValueError:
            min_members = 1
        try:
            max_members = max(min_members, int(request.form.get('max_members', min_members)))
        except ValueError:
            max_members = min_members
        active = request.form.get('active') == 'on'

        update = {
            "event_name": event_name,
            "require_team_name": require_team_name,
            "min_members": min_members,
            "max_members": max_members,
            "active": active,
            "updated_at": datetime.utcnow()
        }
        try:
            events_collection.update_one({"_id": oid}, {"$set": update})
            flash("Event updated.", "success")
        except pymongo_errors.DuplicateKeyError:
            flash("Event name conflicts with existing event.", "error")
        except Exception as e:
            app.logger.exception("Error updating event")
            flash(f"Error: {e}", "error")
        return redirect(url_for('admin_events'))

    return render_template('admin_edit_event.html', event=doc_to_json(event))

@app.route('/admin/event/<event_id>/delete', methods=['POST'])
@admin_required
def admin_delete_event(event_id):
    try:
        oid = ObjectId(event_id)
    except Exception:
        flash("Invalid event id.", "error")
        return redirect(url_for('admin_events'))
    try:
        events_collection.delete_one({"_id": oid})
        flash("Event deleted.", "success")
    except Exception as e:
        app.logger.exception("Error deleting event")
        flash(f"Error: {e}", "error")
    return redirect(url_for('admin_events'))

# -------------------------
# Admin: Registered teams view (legacy name preserved)
# -------------------------
@app.route('/view_registered_teams')
@admin_required
def view_registered_teams():
    try:
        teams_cursor = teams_collection.find().sort("created_at", -1)
        teams = [doc_to_json(t) for t in teams_cursor]
    except Exception as e:
        app.logger.exception("Error fetching teams")
        teams = []
    # Keep using registered_details.html (you already have it)
    return render_template('registered_details.html', teams=teams)

# Admin teams list with search / pagination (new nicer route)
@app.route('/admin/teams')
@admin_required
def admin_teams():
    q = request.args.get('q', '').strip()
    try:
        page = max(1, int(request.args.get('page', 1)))
        per_page = max(5, int(request.args.get('per_page', 10)))
    except ValueError:
        page, per_page = 1, 10

    mongo_filter = {}
    if q:
        regex = {"$regex": q, "$options": "i"}
        mongo_filter = {
            "$or": [
                {"team_name": regex},
                {"team_lead_name": regex},
                {"team_lead_email": regex},
                {"team_lead_reg_no": regex},
                {"members.name": regex},
                {"members.email": regex}
            ]
        }

    total = teams_collection.count_documents(mongo_filter)
    skip = (page - 1) * per_page
    cursor = teams_collection.find(mongo_filter).sort("created_at", -1).skip(skip).limit(per_page)
    teams = [doc_to_json(t) for t in cursor]
    pages = max(1, (total + per_page - 1) // per_page)
    return render_template('admin_teams.html', teams=teams, q=q, page=page, per_page=per_page, total=total, pages=pages)

# Admin: view / edit / delete single team (re-use edit template)
@app.route('/admin/team/<team_id>')
@admin_required
def admin_view_team(team_id):
    try:
        oid = ObjectId(team_id)
    except Exception:
        flash("Invalid team id.", "error")
        return redirect(url_for('admin_teams'))
    team = teams_collection.find_one({"_id": oid})
    if not team:
        flash("Team not found.", "error")
        return redirect(url_for('admin_teams'))
    return render_template('edit_team.html', team=doc_to_json(team), view_only=True)

@app.route('/admin/team/<team_id>/edit', methods=['GET', 'POST'])
@admin_required
def admin_edit_team(team_id):
    try:
        oid = ObjectId(team_id)
    except Exception:
        flash("Invalid team id.", "error")
        return redirect(url_for('admin_teams'))

    if request.method == 'POST':
        data = {
            "team_name": request.form.get('team_name', '').strip(),
            "team_lead_name": request.form.get('team_lead_name', '').strip(),
            "team_lead_email": request.form.get('team_lead_email', '').strip(),
            "team_lead_phone": request.form.get('team_lead_phone', '').strip(),
            "team_lead_reg_no": request.form.get('team_lead_reg_no', '').strip(),
            "updated_at": datetime.utcnow()
        }
        # update members if present (flattened approach)
        members = []
        i = 1
        while True:
            key_name = f"member_{i}_name"
            if key_name not in request.form:
                break
            name = request.form.get(key_name, "").strip()
            email = request.form.get(f"member_{i}_email", "").strip()
            reg = request.form.get(f"member_{i}_reg_no", "").strip()
            if name or email or reg:
                members.append({"name": name, "email": email, "reg_no": reg})
            i += 1

        if members:
            data["members"] = members

        try:
            result = teams_collection.update_one({"_id": oid}, {"$set": data})
            if result.matched_count == 0:
                flash("Team not found.", "error")
            else:
                flash("Team updated successfully.", "success")
        except Exception as e:
            app.logger.exception("Error updating team")
            flash(f"Error updating team: {e}", "error")
        return redirect(url_for('admin_teams'))

    team = teams_collection.find_one({"_id": oid})
    if not team:
        flash("Team not found.", "error")
        return redirect(url_for('admin_teams'))
    return render_template('edit_team.html', team=doc_to_json(team), view_only=False)

@app.route('/admin/team/<team_id>/delete', methods=['POST'])
@admin_required
def admin_delete_team(team_id):
    try:
        oid = ObjectId(team_id)
    except Exception:
        flash("Invalid team id.", "error")
        return redirect(url_for('admin_teams'))
    try:
        result = teams_collection.delete_one({"_id": oid})
        if result.deleted_count == 0:
            flash("Team not found or already deleted.", "error")
        else:
            flash("Team deleted successfully.", "success")
    except Exception as e:
        app.logger.exception("Error deleting team")
        flash(f"Error deleting team: {e}", "error")
    return redirect(url_for('admin_teams'))

# -------------------------
# Export endpoints (Excel / CSV)
# -------------------------
@app.route('/export_excel')
@admin_required
def export_excel():
    return _export_teams(format='excel')

@app.route('/admin/teams/export')
@admin_required
def admin_export_teams():
    fmt = request.args.get('format', 'excel').lower()
    return _export_teams(format=fmt)

def _export_teams(format='excel'):
    teams_list = list(teams_collection.find().sort("created_at", -1))
    serializable = [doc_to_json(t) for t in teams_list]
    if format == 'csv':
        si = BytesIO()
        writer = csv.writer(si)
        if serializable:
            keys = list(serializable[0].keys())
            writer.writerow(keys)
            for row in serializable:
                writer.writerow([row.get(k, "") for k in keys])
        si.seek(0)
        return send_file(si, download_name='teams.csv', as_attachment=True, mimetype='text/csv')
    else:
        df = pd.DataFrame(serializable) if serializable else pd.DataFrame()
        output = BytesIO()
        with pd.ExcelWriter(output, engine='xlsxwriter') as writer:
            df.to_excel(writer, index=False, sheet_name='Teams')
        output.seek(0)
        return send_file(output, download_name="team_details.xlsx", as_attachment=True)

# -------------------------
# Stats endpoints
# -------------------------
def _compute_stats():
    total = teams_collection.count_documents({})
    today_utc = datetime.utcnow().date()
    today_count = teams_collection.count_documents({
        "created_at": {
            "$gte": datetime(today_utc.year, today_utc.month, today_utc.day),
            "$lt": datetime(today_utc.year, today_utc.month, today_utc.day, 23, 59, 59, 999999)
        }
    })
    return total, today_count

@app.route('/admin/stats')
@admin_required
def admin_stats():
    total, today_count = _compute_stats()
    return jsonify({"total": total, "today": today_count})

@app.route('/stats')
def public_stats():
    total, today_count = _compute_stats()
    return jsonify({"total": total, "today": today_count})

# -------------------------
# Download info (keeps original behaviour)
# -------------------------
@app.route('/download_info', methods=['POST'])
def download_info():
    team_info = request.form
    download_content = f"""
Team Name: {team_info.get('team_name','')}
Team Lead: {team_info.get('team_lead_name','')}
Team Lead Email: {team_info.get('team_lead_email','')}
Team Lead Phone: {team_info.get('team_lead_phone','')}
Team Lead Registration Number: {team_info.get('team_lead_reg_no','')}
Member 1: {team_info.get('member_1_name','')} ({team_info.get('member_1_email','')}) | Reg No: {team_info.get('member_1_reg_no','')}
Member 2: {team_info.get('member_2_name','')} ({team_info.get('member_2_email','')}) | Reg No: {team_info.get('member_2_reg_no','')}
Member 3: {team_info.get('member_3_name','')} ({team_info.get('member_3_email','')}) | Reg No: {team_info.get('member_3_reg_no','')}
"""
    return Response(download_content, mimetype="text/plain",
                    headers={"Content-Disposition": "attachment;filename=team_registration.txt"})

# -------------------------
# Legacy: expose raw legacy collection if needed (read-only)
# -------------------------
@app.route('/legacy_teams')
@admin_required
def legacy_teams():
    try:
        docs = [doc_to_json(d) for d in legacy_collection.find().sort("created_at", -1)]
    except Exception:
        docs = []
    return render_template('legacy_teams.html', teams=docs)

# -------------------------
# App start
# -------------------------
if __name__ == '__main__':
    init_db()
    app.run(debug=True)
