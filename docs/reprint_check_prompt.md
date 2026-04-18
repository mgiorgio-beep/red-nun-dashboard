# Add "Reprint Check" feature to payroll

Add the ability to reprint a single check from the run detail view, with a choice between reprinting the original check number or voiding and reissuing with a new number.

## 1. Backend — `routes/payroll_routes.py`

Add a new endpoint **after** the existing `print_run_checks` route (the `POST /api/payroll/runs/<int:run_id>/print-checks` one). Do not modify any existing code — this is purely additive.

```python
# ─────────────────────────────────────────────
#  REPRINT A SINGLE CHECK
# ─────────────────────────────────────────────

@payroll_bp.route("/api/payroll/checks/<int:check_id>/reprint", methods=["POST"])
@admin_required
def reprint_single_check(check_id):
    """
    Regenerate the PDF for a single payroll check.
    Body: {"reissue": true|false}
      - reissue=false: reprint with the same check number
      - reissue=true:  mark old check number as voided, assign next number from
                       check_config, and regenerate PDF with the new number
    Returns JSON with pdf_url pointing at a one-check PDF.
    """
    import sys as _sys, os as _os
    _cp_dir = _os.path.join(_os.path.dirname(__file__), "..", "scripts", "archive")
    if _cp_dir not in _sys.path:
        _sys.path.insert(0, _os.path.abspath(_cp_dir))
    try:
        from check_printer import generate_batch_payroll_checks_pdf as _gen_pdf
    except ImportError:
        return jsonify({"error": "check_printer module not available"}), 500

    payload = request.get_json(silent=True) or {}
    reissue = bool(payload.get("reissue", False))

    conn = get_connection()
    check = conn.execute("SELECT * FROM payroll_checks WHERE id=?", (check_id,)).fetchone()
    if not check:
        conn.close()
        return jsonify({"error": "Check not found"}), 404
    check = dict(check)

    if (check.get("payment_method") or "Manual").lower() == "direct deposit":
        conn.close()
        return jsonify({"error": "Cannot reprint a direct deposit — no paper check exists"}), 400

    if not check.get("net_pay") or check["net_pay"] <= 0:
        conn.close()
        return jsonify({"error": "Cannot reprint a zero-net check"}), 400

    run = conn.execute("SELECT * FROM payroll_runs WHERE id=?",
                       (check["payroll_run_id"],)).fetchone()
    if not run:
        conn.close()
        return jsonify({"error": "Parent payroll run not found"}), 404
    run = dict(run)
    location = check["location"] or run["location"]

    config = conn.execute("SELECT * FROM check_config WHERE location=?", (location,)).fetchone()
    if not config:
        config = conn.execute("SELECT * FROM check_config ORDER BY id LIMIT 1").fetchone()
    if not config:
        conn.close()
        return jsonify({"error": "Check config not set up for this location"}), 400
    config_dict = dict(config)

    old_check_num = check.get("check_number")
    check_num = old_check_num

    if reissue:
        next_check = config_dict.get("check_number_next") or 2001
        check_num = str(next_check)
        # Mark old check as voided, but keep a note so we don't lose the audit trail.
        if old_check_num:
            try:
                conn.execute("""
                    UPDATE payroll_checks
                    SET voided = 1,
                        voided_reason = COALESCE(voided_reason, '') || ?,
                        updated_at = datetime('now')
                    WHERE id = ?
                """, (f"Reissued as #{check_num} on " + datetime.now().strftime("%Y-%m-%d"),
                      check_id))
            except Exception:
                # voided / voided_reason columns may not exist in older schemas —
                # fall back to a status update so the reissue still proceeds.
                conn.execute("UPDATE payroll_checks SET status='voided', updated_at=datetime('now') WHERE id=?",
                             (check_id,))
            # Insert a fresh row representing the new check
            cur = conn.execute("""
                INSERT INTO payroll_checks
                (payroll_run_id, employee_name, check_number,
                 gross_pay, net_pay, wages, paycheck_tips, cash_tips,
                 ee_taxes, er_taxes, deductions, total_hours,
                 pay_period_start, pay_period_end, payment_method,
                 location, status, created_at, updated_at)
                VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,'printed',datetime('now'),datetime('now'))
            """, (check["payroll_run_id"], check["employee_name"], check_num,
                  check["gross_pay"], check["net_pay"], check.get("wages") or 0,
                  check.get("paycheck_tips") or 0, check.get("cash_tips") or 0,
                  check.get("ee_taxes") or 0, check.get("er_taxes") or 0,
                  check.get("deductions") or "{}", check.get("total_hours") or 0,
                  check.get("pay_period_start"), check.get("pay_period_end"),
                  check.get("payment_method") or "Manual", location))
            new_check_id = cur.lastrowid
        else:
            # No old number — just stamp this row with the new one
            conn.execute("UPDATE payroll_checks SET check_number=?, updated_at=datetime('now') WHERE id=?",
                         (check_num, check_id))
            new_check_id = check_id

        conn.execute("UPDATE check_config SET check_number_next=? WHERE location=?",
                     (next_check + 1, location))
        target_check = conn.execute("SELECT * FROM payroll_checks WHERE id=?",
                                    (new_check_id,)).fetchone()
        target_check = dict(target_check)
    else:
        if not old_check_num:
            conn.close()
            return jsonify({"error": "No check number on file — use reissue=true to assign one"}), 400
        target_check = check

    # Build the one-item payroll_list the PDF generator expects
    payroll_list = [{
        "payroll": {
            "id":               target_check["id"],
            "employee_name":    target_check["employee_name"],
            "gross_pay":        target_check["gross_pay"],
            "net_pay":          target_check["net_pay"],
            "wages":            float(target_check.get("wages") or 0),
            "paycheck_tips":    float(target_check.get("paycheck_tips") or 0),
            "cash_tips":        float(target_check.get("cash_tips") or 0),
            "ee_taxes":         float(target_check.get("ee_taxes") or 0),
            "deductions":       target_check.get("deductions") or "{}",
            "total_hours":      float(target_check.get("total_hours") or 0),
            "pay_period_start": run["pay_period_start"],
            "pay_period_end":   run["pay_period_end"],
            "printed_at":       run["pay_date"],
            "location":         location,
        },
        "check_number": target_check["check_number"],
    }]

    os.makedirs(PAYROLL_DIR, exist_ok=True)
    ts = datetime.now().strftime("%Y%m%d%H%M%S")
    pdf_path = os.path.join(
        PAYROLL_DIR,
        f"reprint_{location}_{target_check['check_number']}_{ts}.pdf"
    )

    try:
        _gen_pdf(payroll_list, config_dict, pdf_path)
    except Exception as ex:
        conn.rollback()
        conn.close()
        return jsonify({"error": f"PDF generation failed: {ex}"}), 500

    conn.execute("""
        UPDATE payroll_checks SET status='printed', printed_at=datetime('now'),
               updated_at=datetime('now')
        WHERE id=?
    """, (target_check["id"],))
    conn.commit()
    conn.close()

    return jsonify({
        "status":          "ok",
        "reissued":        reissue,
        "old_check_number": old_check_num if reissue else None,
        "new_check_number": target_check["check_number"],
        "pdf_url":         f"/api/payroll/checks/{target_check['id']}/reprint-pdf?ts={ts}",
        "pdf_path":        pdf_path,
    })


@payroll_bp.route("/api/payroll/checks/<int:check_id>/reprint-pdf")
@login_required
def download_reprint_pdf(check_id):
    """
    Serve the most recent reprint PDF for a given check.
    Finds the newest reprint_*_<check_number>_*.pdf file in PAYROLL_DIR.
    """
    import glob
    conn = get_connection()
    check = conn.execute("SELECT * FROM payroll_checks WHERE id=?", (check_id,)).fetchone()
    conn.close()
    if not check:
        return jsonify({"error": "Check not found"}), 404
    check = dict(check)
    pattern = os.path.join(
        PAYROLL_DIR,
        f"reprint_{check['location']}_{check['check_number']}_*.pdf"
    )
    matches = sorted(glob.glob(pattern))
    if not matches:
        return jsonify({"error": "Reprint PDF not found — generate one first"}), 404
    fname = f"Check_{check['check_number']}_{check['employee_name'].replace(' ', '_')}.pdf"
    return send_file(matches[-1], as_attachment=True, download_name=fname)
```

## 2. Schema — make sure voided columns exist

Extend `init_payroll_tables()` to add the `voided` and `voided_reason` columns if they don't exist. Add this inside that function, alongside the other `ALTER TABLE payroll_checks ADD COLUMN` calls:

```python
for col, defn in [
    ("voided",        "INTEGER DEFAULT 0"),
    ("voided_reason", "TEXT"),
]:
    try:
        conn.execute(f"ALTER TABLE payroll_checks ADD COLUMN {col} {defn}")
    except Exception:
        pass
```

## 3. Frontend — payroll run detail view

Find the page/template that renders the per-check table for a run (likely in `web/static/payroll.html` or similar — search for `payroll_run_id` or `payroll/runs` in the frontend). For each row that has a check number and is not a direct deposit, add a "Reprint" button in the actions column.

Button handler:

```javascript
async function reprintCheck(checkId, employeeName, checkNumber) {
  const choice = prompt(
    `Reprint check #${checkNumber} for ${employeeName}?\n\n` +
    `Type "same" to reprint with the SAME check number.\n` +
    `Type "new" to VOID #${checkNumber} and reissue with a NEW number.\n` +
    `Anything else cancels.`
  );
  if (!choice) return;
  const cleaned = choice.trim().toLowerCase();
  let reissue;
  if (cleaned === "same") reissue = false;
  else if (cleaned === "new") reissue = true;
  else { alert("Cancelled."); return; }

  const resp = await fetch(`/api/payroll/checks/${checkId}/reprint`, {
    method: "POST",
    headers: {"Content-Type": "application/json"},
    body: JSON.stringify({reissue})
  });
  const data = await resp.json();
  if (!resp.ok) { alert(`Error: ${data.error || resp.status}`); return; }

  // Open the PDF in a new tab
  window.open(data.pdf_url, "_blank");

  if (data.reissued) {
    alert(`Check #${data.old_check_number} voided. New check #${data.new_check_number} generated.`);
  }

  // Refresh the row so voided/new status is visible
  if (typeof loadRunDetail === "function") loadRunDetail();
  else location.reload();
}
```

And the button inside the row template, only for paper checks with a number:

```html
<button class="btn btn-sm" onclick="reprintCheck(${check.id}, '${check.employee_name.replace(/'/g, "\\'")}', '${check.check_number}')">
  Reprint
</button>
```

Skip rendering the button if `check.payment_method` is "Direct Deposit" or `check.check_number` is empty.

## 4. Deploy

```bash
cd /opt/red-nun-dashboard
git add routes/payroll_routes.py web/static/payroll.html   # or whatever the frontend file is
git commit -m "Add single-check reprint (same # or void + reissue)"
git push
sudo systemctl restart rednun
```

## Notes

- Uses `admin_required` on POST, `login_required` on the PDF download — matches the existing pattern in the file.
- Reissue path creates a NEW payroll_checks row with the new number so YTD / summary CSVs stay accurate — the old row is marked voided but preserved.
- Reprint PDFs land in `/opt/red-nun-dashboard/payroll_runs/reprint_<loc>_<num>_<ts>.pdf` — won't clobber the original batch PDF.
- The `check_config.check_number_next` counter advances on reissue, same as the initial batch print.
- One-check PDFs use the same `generate_batch_payroll_checks_pdf` function with a one-item list, so layout and MICR are identical to the batch print.
