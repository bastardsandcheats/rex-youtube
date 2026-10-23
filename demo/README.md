# Expanded playback walkthrough

[Play the demo](rex-youtube-demo.mp4)

Recorded on 23 October 2026 against Rex `3bdf52bcb2e13ad414b06133768b15d6a753b1fc` with the included manifest MIME patch. The current site libraries are pinned. Each site library now contains multiple creators; the recording browses the library, a creator, its publication-year season and an episode, then shows several seconds of verified Plex Web playback.

| Site | Creator played |
| --- | --- |
| YouTube | Computerphile |
| Vimeo | Linux Foundation |
| Dailymotion | Clubic |
| PeerTube | Blender Open Movies |
| TikTok | NASA |
| Twitch VOD | bashbunni |

The final section browses the shared **Live Streams** library, then shows actual decoded playback from NASA’s ISS stream, Monterey Bay Aquarium and Brooks Falls through Rex’s native Part endpoint. These clips are explicitly labelled native playback: Plex Web live playback still stalled in the checks. The TikTok weather stream remains available in the library and has passed a native decode check, but is not shown playing in this demo.

This is a silent edited recording. Captured browsing views are held briefly for readability; playback clips run at normal speed. Loading and account-selection screens are excluded, and the account header is covered. Only the final demonstration clips are saved: the service itself keeps no video cache.

All six included browser playback segments advanced with decoded video dimensions and no media error. YouTube and Twitch also exercise seeking. A TechRadar upload stalled during an earlier attempt; Clubic passed the retry used here. Vimeo continuous playback passed, while a far seek stalled in a separate check. Bilibili is omitted because anonymous extraction is blocked on the test host. See the main README for the compatibility matrix and buffering checks.
