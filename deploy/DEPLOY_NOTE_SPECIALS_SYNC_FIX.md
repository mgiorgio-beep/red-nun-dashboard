# Deploy note — Specials sync fix (2026-07-25)

**Copy one file, restart:**

```bash
scp staff/templates/specials_edit.html rednun-beelink:/opt/rednun/staff/templates/specials_edit.html
ssh rednun-beelink "systemctl restart rednun"
```

**What it fixes:** Toast sync on the staff specials editor was appending new specials under the old ones — old ones had to be deleted by hand. Now IMPORT SELECTED replaces the board's specials list when any specials are checked. Soup/app slots still overwrite as before; unchecking all specials in the popup leaves the existing board untouched.

**Verify:** Open `/staff/specials/edit`, hit SYNC FROM TOAST, import — old specials should be gone, only the new ones on the board. Hit publish and check the TV.
