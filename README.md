# AceRadio

An AI-first web radio built on top of **[ACE-Step V1.5](https://github.com/ace-step/ACE-Step-1.5)**.


<img
  src="https://github.com/robustini/AceRadio/blob/main/docs/images/aceradio_01.png"
  alt="AceRadio admin dashboard"
  title="AceRadio admin dashboard"
  style="display: inline-block; margin: 0 auto; max-width: 200px">

AceRadio does **not** replace ACE-Step and does **not** reimplement the generation engine.  
It is a radio orchestration layer built on top of the ACE-Step runtime: the UI defines the station, the backend prepares tracks, the playout logic keeps the broadcast moving, and ACE-Step generates the actual music.

AceRadio is built for a different workflow than a classic one-shot generation page.  
Instead of preparing a single song manually every time, you define a **station identity** and let the system keep building and rotating music around it.

What AceRadio can do in practice:

- continuously generate songs with **ACE-Step**
- use **Ollama** to prepare titles, descriptions, style direction, BPM, duration, key/scale, vocal language, and lyrics
- offload and reload the music runtime around the **Ollama** stage to reduce unnecessary peak VRAM overlap
- work without the Ollama writing stage when you want to rely on internal catalogs, custom catalogs, or already generated tracks on disk
- maintain a **prepared reservoir** of upcoming tracks instead of generating only at the last second
- switch between **AI generated**, **Local catalog**, and **Hybrid** station modes
- reuse songs already present in `aceradio_outputs`
- keep both a rolling generated-song history and dated daily generated-song files for AI-created tracks
- work with **LoRAs** from a catalog and from the ACE-Step `lora` folder
- expose a separate **admin page** and **listener page**
- run as a largely **automatic radio** once started: current track, next track, reservoir refill, transitions, and optional jingle/separator logic are supervised in the background
- provide a **dual-deck admin view** with local prelisten, deck download, waveform display, speed control, and crossfader / auto-fade controls
- let listeners switch between **live listening** and a **free player / browsing** mode
- let listeners vote, browse, seek inside a track from the waveform, and download songs
- publish the radio to external servers via **Icecast, Shoutcast, RTMP, or SRT**
- save and reload station settings from JSON files

In plain English:

- **ACE-Step is the engine**
- **AceRadio is the radio station layer**

---

## 🚀 Installation into an existing ACE-Step root

AceRadio is meant to be installed **from inside the root of your ACE-Step repository**.

So before running the installer command, make sure your shell is already inside your local **ACE-Step V1.5** folder.

The installer then downloads the required AceRadio files automatically from this repository and places them in the correct location inside your ACE-Step tree.

It is expected to copy only the AceRadio payload needed by the integration, not the whole repository.  
In particular, it should **not** overwrite the main `README.md` already present in your ACE-Step root.

### Windows

On Windows, the installer script itself is a **PowerShell** script, but you do **not** have to open PowerShell manually if you do not want to.

You can run the same installer command either:

- from **PowerShell**
- from a regular **Command Prompt (`cmd.exe`)**

In both cases, make sure you are already inside the root of your **ACE-Step** folder before launching it.

#### Windows (PowerShell)

```powershell
powershell -ExecutionPolicy Bypass -Command "irm https://raw.githubusercontent.com/robustini/AceRadio/main/installer/install_aceradio.ps1 | iex"
```

#### Windows (Command Prompt)

```bat
powershell -ExecutionPolicy Bypass -Command "irm https://raw.githubusercontent.com/robustini/AceRadio/main/installer/install_aceradio.ps1 | iex"
```

### Linux

Open a shell inside the root of your ACE-Step folder and run:

```bash
bash -c "$(curl -fsSL https://raw.githubusercontent.com/robustini/AceRadio/main/installer/install_aceradio.sh)"
```

### Important note

These commands are **not** meant to be launched from an arbitrary folder.

Run them only when your current working directory is already the **ACE-Step root**, so the installer can place AceRadio in the expected project structure.

On Windows, this does **not** mean you must work in a PowerShell window all the time.  
It only means that the installer logic is executed by PowerShell, even if you launch it from a normal Command Prompt.

### What the installer should place inside ACE-Step

The intended installation target is the AceRadio integration itself:

```text
acestep/
  ui/
    aceradio/
start_aceradio_ui.bat
start_aceradio_ui.sh
```

The repository `README.md` is meant for the AceRadio GitHub page and should remain there.  
It should **not** be copied over the existing ACE-Step `README.md`.

If you want a local AceRadio document inside the installed tree, keep it under the AceRadio folder itself, for example:

```text
acestep/ui/aceradio/README.md
```

That way, ACE-Step keeps its own root README untouched, while AceRadio can still ship its own documentation in its own folder.

---

## Launchers and runtime setup

AceRadio includes dedicated launchers:

- `start_aceradio_ui.bat` → Windows launcher
- `start_aceradio_ui.sh` → Linux shell launcher with VRAM presets and a simple startup wizard

The application itself starts with:

```bash
python -m acestep.ui.aceradio.run --host 0.0.0.0 --port 7862
```

By default, the admin page is served on:

```text
http://0.0.0.0:7862/
```

and the listener page is served on:

```text
http://0.0.0.0:7862/listen
```

### What the launcher configures

AceRadio does not expose every runtime choice in the web UI.  
Some important decisions are made before the browser opens, directly in the launcher.

The launchers set or expose values such as:

- `ACESTEP_REMOTE_CONFIG_PATH`
- `ACESTEP_REMOTE_LM_MODEL_PATH`
- `ACESTEP_REMOTE_DEVICE`
- `ACESTEP_REMOTE_RESULTS_DIR`
- `ACESTEP_REMOTE_LM_BACKEND`
- `ACESTEP_REMOTE_INIT_LLM`
- `ACERADIO_OLLAMA_MODEL`
- `ACERADIO_OLLAMA_BASE_URL`
- `ACERADIO_AUDIO_FORMAT`
- `ACERADIO_AUTH_ENABLED`
- `ACERADIO_SESSION_SECURE`
- `ACERADIO_CLEANUP_TTL_SECONDS`

### Linux launcher presets

The shell launcher offers these presets:

- **8 GB** → stronger offload / tighter runtime profile
- **12–16 GB** → balanced setup
- **24 GB+** → least constrained preset
- **Custom** → manual values

It also saves the last used configuration in `.aceradio_last`, so the next launch can reuse it.

### Recommended use

For normal use and redistribution, prefer:

- `start_aceradio_ui.bat` on Windows
- `start_aceradio_ui.sh` on Linux

---

## 🧠 VRAM optimization and model load / offload behavior

AceRadio includes a specific VRAM-saving behavior for the **Ollama-assisted** workflow.

When the station is using the AI-written song path, AceRadio does **not** keep the music runtime and the Ollama prompt stage stacked together unnecessarily.  
Before asking Ollama for the next structured song package, AceRadio explicitly asks the ACE-Step side to **offload the music runtime**.  
Once the Ollama step is finished, it asks the music runtime to **load back in** before audio generation starts.

In other words:

1. AceRadio prepares to ask Ollama for the next song brief
2. the music runtime is offloaded first
3. Ollama generates the structured song prompt
4. the Ollama model is told to unload after the request
5. AceRadio reloads the music runtime
6. ACE-Step generates the audio

This reduces unnecessary peak VRAM overlap between the LM-assisted planning phase and the music-generation phase.

### When this behavior applies

This load / offload cycle is relevant when AceRadio is actually using **Ollama** to write the next song package.

If you work with local sources instead, AceRadio can avoid that Ollama prompt-writing stage entirely:

- **Local catalog** uses local JSON song sources instead of asking Ollama to invent the next track
- **Hybrid** mixes local material and AI-written prompts
- cached / already generated material on disk can also be reused by the radio

So if your goal is to minimize VRAM pressure as much as possible, the lightest path is to avoid the Ollama writing stage and rely on local sources or cached material whenever that fits your station design.

### VRAM cleanup modes

AceRadio also exposes a **VRAM cleanup mode** in the UI/runtime settings:

- **fast** → basic CUDA cache cleanup
- **balanced** → basic cleanup plus extra CUDA IPC collection
- **aggressive** → stronger cleanup, including synchronization

That cleanup is used after heavy stages complete, and the Ollama unload path also triggers an aggressive cleanup pass.

---

## ✨ What AceRadio Is For

AceRadio exists to make **continuous AI radio playback** practical from a browser.

It is not just a prompt form on top of ACE-Step.  
It combines station identity, generation planning, playout, catalog fallback, listener playback, and streaming in a single interface.

The station can be driven by:

- a creative **station direction**
- a **negative direction** describing what the radio should avoid
- selected genres, themes, and vocal languages
- duration rules and instrumental probability
- LoRA availability and probability
- AI generation, local catalogs, or both

That makes AceRadio useful for:

- always-on AI music stations
- themed web radios
- hybrid radio setups mixing generated tracks and local JSON catalogs
- private or public live streams backed by ACE-Step

---

## 🧭 How the Radio Works

The high-level flow is:

1. You define the station identity and runtime settings
2. AceRadio prepares the next editorial target for the station
3. If the station is using the AI-written path, **Ollama** generates a structured song package
4. If the station is using local sources, AceRadio pulls the next prompt from its bundled or generated catalog data instead
5. **ACE-Step** generates the audio when a new track must be rendered
6. AceRadio writes metadata and stores the result in `aceradio_outputs`
7. The track enters the radio rotation
8. The supervisor keeps current, next, and reservoir tracks moving
9. Admin and listener pages poll status and stay synchronized

This is why AceRadio behaves more like a small automated station than a classic “generate one file now” interface.

### Fully automatic radio behavior after start

Once the radio is started, AceRadio is designed to keep going on its own.

The backend supervisor handles things such as:

- promoting the next prepared track into the on-air slot
- keeping a reservoir of prepared tracks ready
- triggering refill when the prepared queue drops below threshold
- reusing cached tracks already found in `aceradio_outputs`
- applying automatic transition logic
- arming and firing optional separators / overlays when that part is configured

In other words, the admin page is the control room, but the radio itself is meant to keep running automatically after launch instead of requiring constant manual intervention.

### Admin page and listener page

AceRadio has two distinct pages.

#### Admin page: `/`
![AceRadio listener page](https://github.com/robustini/AceRadio/blob/main/docs/images/aceradio_02.png "AceRadio listener page")

The control room for the station.

This is where you manage:

- station direction and negative direction
- active genres, themes, and vocal languages
- language rotation mode
- generation mode, catalog source, and hybrid file percentage
- custom JSON catalog override
- DiT model and audio-generation values
- LM / Ollama planning controls
- LoRAs and LoRA use probability
- reservoir, cache, VRAM cleanup, and storage limits
- live streaming
- optional jingles / separators
- dual-deck playback, local prelisten, auto-fade, and speed controls
- settings save/load and storage maintenance

#### Listener page: `/listen`
![AceRadio listener page](https://github.com/robustini/AceRadio/blob/main/docs/images/aceradio_03.png "AceRadio listener page")

The public-facing player page.

It is not limited to a passive live stream view.

In normal live mode it follows the current broadcast state, but it can also switch into a **free player / browsing mode** where listeners can:

- move between tracks with the side arrows near the transport controls
- browse the available track list more freely
- click the waveform to jump to a specific point in the song
- use the seek bar directly
- download the selected track
- vote for the current track
- return to the live stream state when they want to reattach to the real broadcast


## 🧠 Ollama and ACE-Step integration

AceRadio uses two separate layers.

### ACE-Step

ACE-Step is the audio generation engine.  
AceRadio mounts and talks to an embedded ACE-Step runtime internally.

That runtime provides the core generation path, model/options lookup, and LoRA-aware job execution.

### Ollama

Ollama is the radio’s writing and planning layer.

In the code, AceRadio asks Ollama to prepare a structured result that can include fields such as:

- title
- description
- style
- BPM
- duration
- key / scale
- vocal language
- lyrics

So the practical split is:

- **Ollama decides what the next song should be**
- **ACE-Step turns that into audio**

If Ollama is not reachable at the configured base URL, the radio can still load and show the UI, but AI-driven track preparation will not behave correctly.

---

## 🎛️ Generation modes

AceRadio exposes three main station modes:

### AI generated

This is the fully AI-driven path.

AceRadio asks Ollama to prepare the next structured song package and then sends that material to ACE-Step for audio generation.

This is the richest mode from an editorial point of view, but it is also the mode where the Ollama-side planning step is actually used.

### Local catalog

Instead of asking Ollama to invent each next song package, AceRadio can pull the next prompt from local JSON song catalogs.

This still keeps the station inside the AceRadio / ACE-Step workflow, but it avoids the Ollama writing stage and is therefore the lightest path from a VRAM point of view.

### Hybrid

This mixes both worlds:

- some tracks use the Ollama-assisted AI-written path
- some tracks come from local catalog files

When Hybrid is active, **File chance %** controls how often the next track should come from the selected local source.

### Catalog sources

When local material is involved, the UI exposes these catalog sources:

- **Library** → the bundled `songs.json`
- **AI catalog** → the generated-song history built in `aceradio_outputs`
- **All local** → combined local pool

There is also an exclusive custom catalog workflow described below.

---

## 📚 Built-in library, generated history, and custom catalogs

AceRadio reads song material from multiple JSON sources.

### Built-in library

The package includes:

```text
acestep/ui/aceradio/songs.json
```

This is the bundled local library.

### Generated-song history

When AceRadio creates songs through the **AI-generated / Ollama-assisted** path, it also writes normalized catalog-style entries back to disk.

The main rolling history file is:

```text
aceradio_outputs/songs.generated.json
```

AceRadio also creates a dated daily file in the same outputs area:

```text
aceradio_outputs/songs.generated_YYMMDD.json
```

So in practice you get:

- one rolling history file that keeps the broader generated-song catalog
- one dated file for the specific day the songs were generated

These generated-song files are used as reusable local source material for future radio rotations.

### What gets written there

The generated-song entry is normalized into catalog-style fields such as:

- title
- description
- style
- lyrics
- BPM
- duration
- key / scale
- time signature
- vocal language

This is why the generated history can be reused later as a local catalog source instead of being just a loose log dump.

### What happens when songs are cleared

When songs are cleared from the outputs area, AceRadio also removes the dated daily generated-song files (`songs.generated_YYMMDD.json`).

So the per-day generated snapshots are cleaned together with the song folders, while the rolling `songs.generated.json` file remains the longer-lived generated catalog.

### Exclusive custom catalog

The admin UI includes an **Exclusive custom catalog** section.

When you load a valid custom catalog:

- AceRadio pulls songs only from that file
- genre, theme, and language filters are bypassed for selection
- the LoRA workflow still remains available

The active custom catalog is stored as:

```text
aceradio_outputs/catalogs/custom_catalog.json
```

Invalid or unusable entries are normalized or ignored, so AceRadio treats this as a curated song source rather than blindly trusting the file contents.

---

## 🎸 LoRA management

AceRadio exposes LoRA selection in the UI and resolves LoRA availability from both a catalog file and the actual LoRA folders on disk.

### Where the catalog lives

The static catalog file is:

```text
acestep/ui/aceradio/lora_catalog.json
```

This file is meant to be edited by the user so it matches the LoRAs that actually exist in their own ACE-Step setup.

The entries shipped in the repository should be treated as example entries only.  
They are not meant to be assumed as universally valid, and they should be adjusted so the catalog lines up with the user's real LoRA folders, ids, labels, and trigger values.

### Where LoRAs are expected on disk

By default, AceRadio resolves LoRAs from the ACE-Step project root:

```text
<project root>/lora
```

unless `ACESTEP_REMOTE_LORA_ROOT` is overridden.

### Practical behavior

AceRadio does not use LoRAs as a decorative label only.

The radio can:

- merge catalog entries with real LoRAs found on disk
- filter enabled LoRAs
- apply a **LoRA use probability**
- keep a practical **single-LoRA-per-song** policy

That makes LoRAs part of the station identity, not just a manual one-off override.

### Per-LoRA weight controls

Each LoRA entry carries its own main LoRA weight together with dedicated advanced weights for:

- **Self-Attention**
- **Cross-Attention**
- **FFN / MLP**

The main weight remains directly editable and the advanced values follow that main weight until they are changed explicitly for the selected LoRA.
Once changed, the edited control becomes a real override while the untouched controls continue to follow the main LoRA weight.

These values are part of the AceRadio station settings for each LoRA entry, survive save/load cycles, and are propagated into the job payload and runtime LoRA application path used by the radio.

All four LoRA weights use the same effective range of **0.0 to 2.0**, including values above `1.0`, so the UI, settings layer, job payload, and runtime module scaling all stay aligned.

---

## 🎚️ Station controls and generation values

The admin page is more than a simple prompt form.  
It is where you define both the **editorial identity** of the station and the **technical behavior** of the radio.

### Station identity and catalog behavior

The station section lets you configure:

- **Station direction** → the main creative brief sent to the LM side
- **Negative direction** → what the station should avoid
- active **genres**
- active **themes**
- available **vocal languages**
- **language rotation mode** (`round_robin` or `random`)
- **Generation Mode** (`AI generated`, `Local catalog`, `Hybrid`)
- **Catalog Source** when local catalog access is enabled
- **File chance %** for Hybrid mode
- **Exclusive custom catalog** override loaded from a JSON file
- **Instrumental %**
- **Min / Max duration**
- **Automatic duration**
- **LoRA use %**
- **Auto transition cut (s)**

### Model and audio-generation controls

The model section exposes the main ACE-Step generation values, including:

- **DiT model**
- **Use ADG**
- **Inference steps**
- **Inference method**
- **Guidance scale**
- **Shift**
- **CFG interval start / end**
- **Normalization target dB**
- **Quality score sensitivity**
- **Latent shift**
- **Latent rescale**
- **timesteps override**
- **output audio format**
- **MP3 bitrate / sample rate** when MP3 is selected

### LM / Ollama controls

AceRadio also exposes a dedicated LM section for the Ollama-side planning phase, including:

- **Enable LM planning**
- **Constrained decoding**
- **CoT metas**
- **Caption rewrite**
- **CoT language**
- **Parallel thinking**
- **Constrained decoding debug**
- **LM temperature**
- **LM CFG scale**
- **LM top-K**
- **LM top-P**
- **LM negative prompt**

These values matter only for the LM-assisted planning side.  
If you run AceRadio from local catalogs or cached material instead, that path is reduced or skipped entirely.

### Runtime, storage, and maintenance

The runtime section includes:

- **VRAM cleanup mode**
- **Reservoir target**
- **Refill threshold**
- **Keep on disk**
- cache status
- **Rebuild cache**

The storage/config area also includes:

- **Save**
- **Save as**
- **Browse** another settings file
- saved-track counters
- cache-on-disk counters
- **Clear cache**
- **Clear all songs**
- admin/runtime error log view and clear action

### Streaming controls

The Live Streaming section lets you configure:

- protocol
- server template / preset
- host
- port
- mountpoint
- username / password
- bitrate
- stream format
- station name / description / genre
- public/private flag
- stream validation
- stream error log

### Jingle controls

When jingles / separators are part of the setup, the admin side also exposes:

- overlay / separator quick launch
- jingle rescan
- per-jingle enable state
- `every_n_songs`
- per-jingle volume
- separator timing before end
- overlay midpoint timing
- fade / duck / restore timing
- queue separator
- stop active jingle
- jingle live status and counters

### Dual-deck admin transport

The admin page is also a real deck interface, with:

- **Deck A** for the current track
- **Deck B** for the next / cued track
- per-deck waveform
- deck download
- deck play / pause for local preview
- lyrics panes
- playback speed control
- local prelisten mute
- prelisten master level
- crossfader
- auto-fade

That is why AceRadio feels closer to a compact radio playout console than to a minimal song-generation page.


## 🔄 Reservoir, cache, and playout

A central part of AceRadio is the **reservoir**.

Instead of waiting until the current song ends before deciding what to do next, AceRadio tries to keep a prepared supply of tracks ready.

In practice, the runtime tracks:

- the current track
- the next track
- a background reservoir of prepared tracks
- a cache pool of reusable material already on disk

### What happens after the radio starts

After startup, AceRadio supervises the rotation continuously.

It can:

- scan `aceradio_outputs` for reusable cached tracks
- promote a prepared track into the **next** slot
- promote the next track into the **current / on-air** slot
- refill the reservoir when the prepared count falls below the configured threshold
- reject or recycle recently seen tracks when needed
- keep recently played history for safer rotation behavior

### Deck-based control

The admin page also exposes a deck-style layout:

- **Deck A** for the current track
- **Deck B** for the cued / next track
- local preview transport
- local-only mute
- prelisten master level
- playback speed
- crossfader and auto-fade behavior

This is one of the main reasons AceRadio feels like a playout interface rather than a plain generation panel.

### Cache reuse

AceRadio can rescan `aceradio_outputs` and reuse existing tracks as local radio material.

The UI includes cache-oriented tools and status blocks, so previously generated tracks can be brought back into circulation after a restart.

### Automatic transitions

AceRadio is not limited to “wait until the file ends and then do something”.

The runtime keeps track of playback elapsed time and can apply:

- normal end-of-track rotation
- optional **Auto transition cut**
- separator arming near the transition window
- auto-fade / deck-based handoff behavior on the playback side

So once the station is running, track-to-track movement is meant to be automatic, while still leaving manual controls available in the admin UI.


## 🔊 Live streaming

AceRadio can publish the station to an external streaming target.

The admin UI includes a full **Live Streaming** section with fields for:

- protocol
- preset / server mode
- host
- port
- mountpoint
- username / password
- bitrate and format
- station metadata
- public/private flag

### Protocols visible in the code

AceRadio supports:

- **Icecast 2**
- **Shoutcast v1**
- **Shoutcast v2**
- **RTMP**
- **SRT**

There are also built-in presets for several **Listen2MyRadio** scenarios.

### Stream formats

The UI exposes:

- MP3
- AAC
- Opus

### Important requirement

Streaming depends on **ffmpeg** being available in the system `PATH`.

If `ffmpeg` is missing, stream validation and start-up will fail.

---


## 🎙️ Jingles and separators

AceRadio supports optional **overlay** and **separator** jingles.

The expected folders on disk are:

```text
aceradio_jingles/
├─ overlay/
└─ separator/
```

and the main configuration file is:

```text
aceradio_jingles/jingles.json
```

### What the jingle system can do

When jingles are configured, AceRadio can:

- rescan the jingle folders
- keep separate **overlay** and **separator** pools
- enable or disable individual jingles
- use per-jingle `every_n_songs`
- use per-jingle volume
- queue a separator manually
- trigger an overlay or separator manually from the admin page
- automatically arm and fire separators around transitions
- automatically trigger eligible overlays during the body of a track when timing rules match

### Manual and automatic behavior

The admin UI exposes both quick-launch controls and timing controls, but the radio is also able to use this system automatically once it is running.

So jingles are not just a manual button effect.  
If you configure them, they can become part of the normal radio automation.


## 👥 Listener interaction

The listener side is not passive only.

It can behave both as a **live listener page** and as a **free player / browsing interface**.

### Live mode

In live mode, the page stays attached to the actual radio state and follows the current broadcast.

That includes:

- live current track
- live next track
- listener count
- vote state
- transport state aligned with the radio

### Free player / browsing mode

The listener page can leave the pure streaming state and switch into a browsing mode.

From there, users can:

- move backward and forward across tracks with the side arrows near the transport controls
- browse a wider local track list instead of staying locked to the live progression
- click directly on the waveform to jump to a specific point
- use the seek bar for precise seeking
- pause locally without changing the actual radio state
- return to live mode when they want to reattach to the real broadcast

### Waveform, seeking, and download

The listener UI is not limited to a basic HTML audio bar.

It includes:

- waveform rendering
- waveform progress display
- click-to-seek on the waveform
- seek bar control
- track download

### Voting

Listeners can vote for the current song.

The implementation uses browser-local vote tracking and also persists the vote information into track metadata, so the vote state is not lost on a simple refresh.

### Listener count

AceRadio tracks active listeners through ping calls and exposes a live count.


## 🔐 Optional authentication

AceRadio supports optional login protection for the admin side.

Relevant values visible in the code include:

- `ACERADIO_AUTH_ENABLED`
- `ACERADIO_USERNAME`
- `ACERADIO_PASSWORD`
- `ACERADIO_SESSION_SECURE`

Credentials can also be resolved from the saved settings JSON when environment variables are not explicitly set.

### Practical behavior

This is a lightweight admin gate, not a full multi-user management system.

When auth is enabled:

- unauthenticated users are redirected to `/login`
- the admin page is protected
- the listener page and listener-facing endpoints remain reachable so the station can still be consumed

---

## 📦 Output folders, metadata, and settings

By default, AceRadio works inside:

```text
aceradio_outputs/
```

The launchers also point ACE-Step results to that same workspace.

A practical layout looks like this:

```text
aceradio_outputs/
├─ configs/
│  ├─ aceradio_config.json
│  └─ system/
│     └─ last_used_config.json
├─ catalogs/
│  └─ custom_catalog.json
├─ songs.generated.json
├─ songs.generated_YYMMDD.json
├─ <job-id-1>/
│  ├─ metadata.json
│  └─ aceradio_track.json
├─ <job-id-2>/
│  ├─ metadata.json
│  └─ aceradio_track.json
└─ ...
```

### Settings files

AceRadio supports:

- save
- save as
- load
- browse a different settings JSON

The main active settings file is:

```text
aceradio_outputs/configs/aceradio_config.json
```

and the last used settings file is remembered in:

```text
aceradio_outputs/configs/system/last_used_config.json
```

### Local file dialogs

Some settings and custom-catalog browse actions use file dialogs on the host machine.  
So those actions are designed for a locally run AceRadio instance, not for a purely remote browser-only deployment.

---

## 🎵 Audio formats

AceRadio exposes multiple track output formats in the UI/runtime, including:

- MP3
- FLAC
- WAV
- WAV32
- Opus
- AAC

When MP3 is selected, the UI also exposes bitrate and sample-rate options.

The launchers default to:

```text
ACERADIO_AUDIO_FORMAT=mp3
```

---

## 📝 Notes

AceRadio is intentionally a radio workflow layer, not a replacement for ACE-Step internals.

If the UI opens correctly but the station does not actually produce tracks, the problem is usually in one of these areas:

- ACE-Step runtime not ready
- Ollama not reachable at the configured base URL
- missing or invalid model setup
- missing LoRA folders on disk
- invalid custom catalog JSON
- missing `ffmpeg` for streaming
- GPU / VRAM / runtime issues

Also note:

- the listener page is a real part of the product, not a duplicate of the admin page; it has both live and free-player browsing behavior
- local catalogs, generated history, and cache reuse are all first-class parts of the AceRadio workflow
- the Ollama-specific load / offload optimization applies to the AI-written prompt stage, not to every station mode equally
- if you want the leanest runtime behavior, prefer local sources and cached material instead of always going through the Ollama writing step
- the project includes some helper modules related to chord/reference tooling, but the core user-facing identity of AceRadio is the automated radio flow described above

---



## License

AceRadio is distributed under the **GNU General Public License v3.0**.

See [`LICENSE.txt`](LICENSE.txt) for the full license text.

---

## Contributing

If you want to contribute to AceRadio, feel free to open a **Pull Request**.

If you find a bug, a regression, or something unclear in the current behavior, please open an **Issue** and describe the problem as clearly as possible, including steps to reproduce it when relevant.

Suggestions, fixes, cleanup, and practical improvements are welcome.

