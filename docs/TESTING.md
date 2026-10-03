# In-game test plan

The mod was built and checked offline only (see README, "How it is checked"). These tests are the
first time it meets the real game. Run them in order; each step says what to expect and what a
failure most likely means. Record the results in the table at the end.

Console: press `~`, type the command, Enter. Click an NPC while the console is open to select it;
commands without `player.` then act on that NPC.

## 0. Preparation

1. Install SKSE64 2.3.1, Address Library for 1.7.104, SPID and this mod (README, "Install").
2. Optional, for more detail: create `Data\SKSE\Plugins\po3_SpellPerkItemDistributor.ini` with
   ```
   [Log]
   LogLevel = debug
   ```
   (SPID reads this key at start-up; default is `info`, which already logs every NPC it processes.)
3. Make a **new save** to test on, and keep a save from before installing.
4. Start the game with `skse64_loader.exe` (MO2: the *SKSE* executable; CrossOver: run
   `skse64_loader.exe` from the game folder inside the bottle).

Log locations (Windows paths; under CrossOver they are inside the bottle, below
`drive_c/users/<user>/Documents/`):
* `Documents\My Games\Skyrim Special Edition\SKSE\skse64.log` (SKSE, lists loaded plugins)
* `Documents\My Games\Skyrim Special Edition\SKSE\po3_SpellPerkItemDistributor.log` (SPID)

## 1. Loading

| # | Check | Expected | If not |
|---|---|---|---|
| 1.1 | main menu reached | no crash, no "missing master" | CTD before the menu: check SKSE/Address Library/SPID versions; disable the plugin to isolate |
| 1.2 | `skse64.log` | SPID loaded, not "disabled" | "address library needs to be updated": install the Address Library release containing `versionlib-1-7-104-0.bin` |
| 1.3 | SPID log, `INI` section | `CardboardFaceMask_DISTR.ini` is listed | the ini is not directly in `Data` or not named `*_DISTR.ini` |
| 1.4 | SPID log once the main menu is reached (SPID resolves forms when the game data has loaded) | `Registered x/y Items` with x = y (1/1 if no other mod distributes items: the count covers every ini); no `FAIL` line mentioning CardboardFaceMask | `FAIL - formID doesn't exist`: plugin not active or renamed |
| 1.5 | SPID log, `INI` section | no `Failed to parse entry [Item = 0x800~CardboardFaceMask.esp...]` warning | the ini line was edited into a form SPID cannot read and is skipped (then 1.4 also has no `Registered ... Items` line unless another mod distributes items). SPID usually prints **no** console message for this; the console's `[SPID] Errors found when reading configs` appears only for rarer errors |

## 2. The item itself (on the player)

1. `help "Cardboard Face Mask" 4` - expected one `ARMO` line with an ID like `FExxx800`
   (`xxx` = the light-plugin index in your load order). Use that ID below.
2. `player.additem FExxx800 1` - expected: "Cardboard Face Mask added", weight 0, value 0 in the
   apparel list (unless `non_playable: true`, then it is hidden).
3. `player.equipitem FExxx800` and switch to third person.

| # | Check | Expected |
|---|---|---|
| 2.1 | front | the photo, upright and **not mirrored** (compare the hair parting and glasses with `input/IMG_2117.PNG`), sharp, no purple |
| 2.2 | edges | thin corrugated cardboard edge visible from the side |
| 2.3 | back | plain kraft brown with tape strips where the twine is attached |
| 2.4 | twine | goes around the back of the head, slightly lopsided and drooping, not cutting through the head or ears |
| 2.5 | movement | turn the camera, walk, run, jump, draw a weapon: the mask stays on the face and turns with the head |
| 2.6 | helmet | equip any helmet or hood: both are visible together (slot 55 vs 30/31/42); note clipping |
| 2.7 | lighting | the front is matte-ish, the back slightly rougher; no mirror-like shine; looks right in daylight and at night |
| 2.8 | distance | walk away 30+ m: no flicker or holes, texture stays clean (mip-maps) |
| 2.9 | first person | note whether the mask (seen from behind) blocks the first-person view. The armatures have no separate first-person model, so the game uses the third-person one; if it blocks the view, report it (only matters when the player wears it) |
| 2.10 | unequip | `player.unequipitem FExxx800` removes it cleanly |

Repeat 2.1-2.6 with a female character if you have one (female mesh is 4 % smaller).

## 3. Distribution to NPCs

Go somewhere busy (a city or an inn). For each NPC below: look at it, then select it in the
console and type `inv` (lists the selected NPC's inventory; the mask should be there once).
Find base IDs for spawning with `help "<name or part of it>" 4` (lines starting `NPC_`) and spawn
with `player.placeatme <baseID> 1`.

| # | NPC | Expected |
|---|---|---|
| 3.1 | adult human male (e.g. a guard or innkeeper) | wears the mask |
| 3.2 | adult human female | wears the (female) mask |
| 3.3 | high elf / dark elf / wood elf / orc | wears the mask |
| 3.4 | Khajiit (male and female) | wears the larger beast mask; snout and ears not poking through badly |
| 3.5 | Argonian (male and female) | same as 3.4 |
| 3.6 | elder (old) NPC | wears the mask |
| 3.7 | guard with a full helmet | mask visible together with the helmet; note clipping |
| 3.8 | child | **no** mask, nothing in `inv` |
| 3.9 | creature (wolf, draugr, dragon) | no mask (no `ActorTypeNPC`) |
| 3.10 | vampire NPC | wears the mask |
| 3.11 | follower / housecarl | wears the mask |
| 3.12 | an NPC spawned with `placeatme` | wears the mask |

If an NPC has the mask in `inv` but does not wear it: with the NPC selected, `equipitem FExxx800`.
* If it then appears: the mesh is fine and the game simply chose not to equip it; note which NPCs
  (unique? leveled? has a hood?) - see docs/SPID_NOTES.md section 9.
* If it stays invisible while equipped: that race is not covered by the armatures (note the race).

## 4. Persistence and repeat loads

| # | Check | Expected |
|---|---|---|
| 4.1 | save, quit to desktop, restart, load | the same NPCs still wear one mask each |
| 4.2 | repeat 4.1 two more times, then `inv` on a few NPCs | still **one** mask each (counts must not grow) |
| 4.3 | change cells (fast travel and back) | masks still there |
| 4.4 | kill a masked NPC, loot the body | "Cardboard Face Mask" in the loot (if playable); the corpse wears it until taken |
| 4.5 | chance: change the installed ini's last field from `100` to `25!`, restart, load the save made before installing (in later saves NPCs may keep masks they already had at 100 %) | roughly a quarter of NPCs masked; the SPID log shows the line without `Failed to parse entry` (with `!` it also logs a `Seed for Actor ...` line per NPC) |
| 4.6 | restart again and load the same save | the **same** NPCs have masks as in 4.5 (`!` = fixed roll per NPC and character) |
| 4.7 | optional: use `25` without `!`, restart twice | the masked NPCs change between launches (SPID re-rolls each launch); restore `100` afterwards |

## 5. Stability

* Spend 10+ minutes in a crowded city and in combat with humanoid enemies (bandits): no crash, no
  big frame-rate drop compared with the mod disabled.
* If a crash happens, disable `CardboardFaceMask.esp`, repeat, and keep the crash log.

## 6. Results

| Step | Result (ok / problem) | Notes (race, NPC name, screenshot file) |
|---|---|---|
| 1.1-1.5 | | |
| 2.1-2.10 | | |
| 3.1-3.12 | | |
| 4.1-4.7 | | |
| 5 | | |

Screenshots of the front, side and back of a masked NPC (human and beast) are the most useful
feedback for tuning `forward_offset_units`, `size_factor` and `scale_beast` in `config.yaml`.
