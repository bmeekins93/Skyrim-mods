# In-game test plan

The mod was built and checked offline only (see README, "How it is checked"). These tests are the
first time it meets the real game. Run them in order; each step says what to expect and what a
failure most likely means. Record the results in the table at the end.

Console: press `~`, type the command, Enter. Click an NPC while the console is open to select it;
commands without `player.` then act on that NPC. Two checks used below, on a selected NPC:
`inv` (short for `showinventory`) lists its inventory, and `getequipped FExxx800` (use the mask
ID from section 2, step 1) tells whether it wears the mask. The second one is **not verified**:
GetEquipped is a condition function (CK wiki: `[ActorID.]GetEquipped ObjectID`, 1 when the actor
has the object equipped, https://ck.uesp.net/wiki/GetEquipped), and many condition functions also
work in the console; expect `1.00` when worn and `0.00` when not. If the console does not accept
it, judge "worn" by looking at the NPC, and note that in the results.

## 0. Preparation

1. Install SKSE64 2.3.1, Address Library for 1.7.104, SPID, KEF (Keyword Equip Framework,
   https://www.nexusmods.com/skyrimspecialedition/mods/185273) and this mod (README, "Install").
   The ini built with the default `equip_method: kef` has two active lines: `Item = ...` (SPID puts
   the mask in the inventory) and `Keyword = KEFEquip_0x800_CardboardFaceMask_esp|...` (KEF makes
   the NPC wear it). What is and is not verified about KEF: docs/SPID_NOTES.md section 13.
   Note the KEF file version and the name of the `.dll` in the KEF download's `SKSE/Plugins`
   folder (its name is not documented here); step 1.2 looks for it in `skse64.log`.
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
| 1.1 | main menu reached | no crash, no "missing master" | CTD before the menu: check SKSE/Address Library/SPID/KEF versions; disable the plugin (then KEF) to isolate |
| 1.2 | `skse64.log` | SPID **and KEF** (the DLL name noted in 0.1) loaded, not "disabled" | "address library needs to be updated": install the Address Library release containing `versionlib-1-7-104-0.bin`; KEF missing or disabled: install/update KEF (note the KEF file version and what the log says - its 1.7.104 support is not verified) |
| 1.3 | SPID log, `INI` section | `CardboardFaceMask_DISTR.ini` is listed | the ini is not directly in `Data` or not named `*_DISTR.ini` |
| 1.4 | SPID log once the main menu is reached (SPID resolves forms when the game data has loaded) | `Registered x/y Items` and `Registered x/y Keywords` with x = y (1/1 each if no other mod distributes items or keywords: the counts cover every ini); no `FAIL` line mentioning CardboardFaceMask or `KEFEquip` | `FAIL - formID doesn't exist`: plugin not active or renamed; `FAIL - couldn't create keyword`: report it (SPID could not create the KEF keyword) |
| 1.5 | SPID log, `INI` section | no `Failed to parse entry [Item = ...]` or `[Keyword = ...]` warning | that line was edited into a form SPID cannot read and is skipped (then 1.4 also lacks its `Registered ...` line unless another mod distributes that type). SPID prints **no** message in game for this; its only in-game text, the console line `[SPID] Errors found when reading configs`, is not triggered by a bad `Item` or `Keyword` line (docs/SPID_NOTES.md section 10) |
| 1.6 | SPID log, per NPC (after loading a save, step 3) | `Distribution for <NPC>` followed by an `ARMO` line with `"Cardboard Face Mask"` and a `KYWD` line with `KEFEquip_0x800_CardboardFaceMask_esp` | only `ARMO`: the `Keyword` line is missing or was skipped (1.5), or the package was built with `equip_method: engine` |

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
console, type `inv` (the mask should be listed; note whether once or twice, see 3.13) and
`getequipped FExxx800` (1.00 = worn; see the top of this page if the console rejects it). Find base IDs for spawning with
`help "<name or part of it>" 4` (lines starting `NPC_`) and spawn with `player.placeatme <baseID> 1`.

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
| 3.13 | mask count in `inv` of a few NPCs | probably **2** (KEF's page indicates that KEF adds the item itself, next to SPID's copy); **1** means KEF did not add a copy to an NPC that already had one (3b.2 tells whether it adds one at all). Record the count per NPC: 4.1-4.3 compare against it. Both are harmless |
| 3.14 | an NPC that wears another item in slot 55 (e.g. another mod's accessory), if you find one | per KEF's page: no mask worn and only SPID's copy in `inv` (KEF skips occupied slots). Optional: append `_forceEquip_true` to the keyword name in the installed ini (`KEFEquip_0x800_CardboardFaceMask_esp_forceEquip_true`), restart: the mask replaces the other item. Restore the ini afterwards |

If an NPC has the mask in `inv` but does not wear it (not on the face; `getequipped FExxx800`
returns 0.00):
* Nobody wears it: KEF is not loaded (1.2) or the `Keyword` line is missing (1.6).
* The NPC wears another slot-55 item: see 3.14.
* Only some other NPCs: leave the cell and come back (KEF reportedly equips when an NPC's 3D
  loads; SPID can distribute after that for some NPCs - unverified), then check again. Note which
  NPCs (unique? leveled? has a hood?).
* With the NPC selected, `equipitem FExxx800`. If it then appears the mesh is fine; if it stays
  invisible while equipped, that race is not covered by the armatures (note the race).

### 3b. Optional: what KEF does on its own

These answer the unverified points in docs/SPID_NOTES.md section 13. Use a save from **before**
installing the mod (inventory changes from an earlier session can stay in later saves), and restore
the installed ini from the zip afterwards.

| # | Change to the installed `CardboardFaceMask_DISTR.ini` | Then check on a few NPCs | Tells you |
|---|---|---|---|
| 3b.1 | put `;` in front of the `Keyword = ...` line (or disable KEF) | `inv` lists the mask, `getequipped` 0.00 on most NPCs | whether the game ever equips the SPID item by itself (users report: no) |
| 3b.2 | put `;` in front of the `Item = ...` line instead | mask in `inv` (once) and worn: KEF adds the item itself (expected from KEF's page); nothing in `inv`: KEF only equips what the NPC already has | whether the `Item` line is needed with KEF |

## 4. Persistence and repeat loads

| # | Check | Expected |
|---|---|---|
| 4.1 | save, quit to desktop, restart, load | the same NPCs still wear a mask, and `inv` shows the same count per NPC as recorded in 3.13 (1 or 2) |
| 4.2 | repeat 4.1 two more times, then `inv` on the same NPCs | still the count recorded in 3.13: it must **not grow** |
| 4.3 | change cells 3+ times (fast travel away and back, which reloads the NPCs' 3D), then `inv` on the same NPCs | masks still worn and the count unchanged (KEF reportedly runs on every 3D load; a growing count means it adds a copy each time: report it with the KEF version) |
| 4.4 | kill a masked NPC, loot the body | "Cardboard Face Mask" in the loot (if playable); the corpse wears it until taken |
| 4.5 | chance: in the installed ini replace the two active lines with the commented "one NPC in four" pair (`Keyword` line ending `25!`, `Item` line filtered on `KEFEquip_0x800_CardboardFaceMask_esp`), restart, load the save made before installing (in later saves NPCs may keep masks they already had at 100 %) | roughly a quarter of NPCs masked, and every NPC with a mask in `inv` also wears it; the SPID log shows both lines without `Failed to parse entry` (with `!` it also logs a `Seed for Actor ...` line per NPC) |
| 4.6 | restart again and load the same save | the **same** NPCs have masks as in 4.5 (`!` = fixed roll per NPC and character) |
| 4.7 | optional: remove the `!` from the `Keyword` line, restart twice | the masked NPCs change between launches (SPID re-rolls each launch); restore the original lines afterwards |

## 5. Stability

* Spend 10+ minutes in a crowded city and in combat with humanoid enemies (bandits): no crash, no
  big frame-rate drop compared with the mod disabled.
* If a crash happens, disable `CardboardFaceMask.esp`, repeat, and keep the crash log.

## 6. Results

| Step | Result (ok / problem) | Notes (race, NPC name, screenshot file) |
|---|---|---|
| 1.1-1.6 | | KEF DLL name and file version (0.1) |
| 2.1-2.10 | | |
| 3.1-3.14 | | masks per NPC in 3.13; whether `getequipped` worked in the console |
| 3b.1-3b.2 | | |
| 4.1-4.7 | | |
| 5 | | |

Screenshots of the front, side and back of a masked NPC (human and beast) are the most useful
feedback for tuning `forward_offset_units`, `size_factor` and `scale_beast` in `config.yaml`.
