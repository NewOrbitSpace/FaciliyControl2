# One-time repo reorganisation (run once, then commit)

I can't run git through the Claude connection, so run this yourself in **PowerShell** from the repo
root (`C:\Users\SelimOlguPilav\Documents\FaciliyControl`). It uses plain moves + `git add -A`, which
works whether or not the files were already tracked (git records the renames at commit time).

The shared code folder `python_facility_control\` is left where it is, so nothing about running the
app changes.

```powershell
cd "C:\Users\SelimOlguPilav\Documents\FaciliyControl"

# 1. folders (chambers\small already has a README from Claude)
New-Item -ItemType Directory -Force chambers\small\labview, tooling | Out-Null

# 2. move the small-chamber LabVIEW source + exports
Move-Item Main_V4.4.*                    chambers\small\labview\
Move-Item "Small_Chamber_Support VIs"    chambers\small\labview\

# 3. move the VI-export tooling out of the root
Move-Item lvkit-wheels, vi2json.py, setup_lvkit.py, README_lvkit.md tooling\

# 4. (optional) drop the scratch outputs folder from the repo; .gitignore also excludes it
#    Remove-Item -Recurse -Force "Claude outputs"

# 5. stage everything (git detects the moves as renames) and commit
git add -A
git status                 # sanity-check what will be committed
git commit -m "Organise repo: shared engine + chambers/small, tooling/, .gitignore"
git push                   # if you want it on the remote
```

After this the layout is:
```
FaciliyControl/
├─ .gitignore
├─ README.md
├─ python_facility_control/        (shared engine – unchanged)
├─ chambers/small/
│   ├─ README.md
│   └─ labview/  (Main_V4.4.* + Small_Chamber_Support VIs)
└─ tooling/      (lvkit-wheels, vi2json.py, setup_lvkit.py, README_lvkit.md)
```
