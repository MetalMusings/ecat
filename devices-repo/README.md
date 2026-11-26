# Device data for EtherCAT Setup (TEST)

**This is a test of a prototype.** It will be removed when the real repository is made.
Do not rely on what is here.

What [EtherCAT Setup](https://github.com/MetalMusings/ecat) knows about EtherCAT
devices beyond their ESI files, written by its users: quirk files. Each fact in a
file says where it comes from (a manual, a test on a machine).

## How it works

- **Sharing.** In EtherCAT Setup, *Device data...* on a slave's page, then *Share...*:
  the window opens a new Discussion here with the file in it (your machine's pin names
  and scales are left out). Sending it needs a GitHub account.
- **The check.** An Action checks every post automatically with EtherCAT Setup's own
  check (format, no machine pins, HAL lines only of plain logic between the device's
  own pins) and keeps the file as `devices/<vendor>-<product>/<discussion>.yaml`. It
  says in the thread what it did. Nobody reviews the files beforehand: **you review
  each fact when you choose it**, one by one, in the window, which checks it again
  against your device.
- **Fetching.** *Fetch shared data* in the window downloads this repository's files
  (only when you ask; no account needed).
- **Reports and votes.** Up-vote a Discussion, comment on it, or send a report from
  the window (*Report...*: worked / partly / did not work, with your own words). You
  can edit your report later or add another. `index.json` is rebuilt from the threads.
- **Removal.** A file whose Discussion is deleted is removed. Report a bad file with
  GitHub's *Report content* or a comment.

## Licence

Everything here is [CC0 1.0](LICENSE): anyone may use it for anything. Cite manuals
(name, version, section); do not copy their text.
