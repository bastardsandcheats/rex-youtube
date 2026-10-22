# Browser walkthrough

[Play the demo](rex-youtube-demo.mp4)

A silent walkthrough of the actual Plex Web UI. Each provider is a show library: browse the creator, its year seasons and episodes, then watch a few seconds of actual playback. Captured library/show/season views are held briefly for readability; playback footage runs at normal speed. Loading and private account-selection screens are excluded.

Order: YouTube (Technology Connections), Vimeo (Business of Software), Dailymotion (ComputerHoy), PeerTube (A propos de PeerTube), TikTok (Samantha Cristoforetti), Twitch VOD (csharpfritz). The examples cover electronics, software, cryptography, open-source video and space science. Browser verification requires advancing video with decoded dimensions and no media error. The YouTube and Twitch recordings also exercise seeking.

Standalone video URLs still map to movies. Live sources from every provider share **Live Streams**; this walkthrough focuses on persistent channel collections. Twitch and TikTok live were separately decoded through Rex's continuous Part endpoint. Plex Web did not advance on the tested TikTok live item, so live browser compatibility remains experimental.

Bilibili is omitted because anonymous extraction is blocked on the test host. Vimeo continuous playback passed; seeking into its HLS stream stalled in a separate browser check.
