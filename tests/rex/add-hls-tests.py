from pathlib import Path

p = Path("/build/rust/src/runtime/feed.rs")
s = p.read_text()
i = s.index("    #[test]", s.index("#[cfg(test)]"))
s = s[:i] + """    #[test]
    fn local_hls_requires_explicit_probe_and_streams() {
        let source = Source::register("test", &json!({"url": "http://127.0.0.1:9"})).unwrap();
        let mut item = json!({"id":"vod", "path":"vod.m3u8"});
        assert!(Item::parse(&source, &item).is_err());
        item["media_info"] = json!({"format":{"format_name":"hls"}, "streams":[]});
        assert!(Item::parse(&source, &item).is_err());
        item["media_info"]["streams"] = json!([{"codec_type":"video", "codec_name":"h264"}]);
        assert!(Item::parse(&source, &item).is_ok());
        assert_eq!(super::super::part_content_type(&json!({"file":"vod.m3u8"})), "application/vnd.apple.mpegurl");
    }

""" + s[i:]
p.write_text(s)
p = Path("/build/rust/src/runtime/universal.rs")
s = p.read_text()
i = s.index("    #[test]", s.index("#[cfg(test)]"))
s = s[:i] + """    #[test]
    fn local_hls_remux_resets_input_codec_tags_and_seeks() {
        let mut p = plan(true, 3732000);
        p.url = "http://relay/video.m3u8".into();
        p.protocol = Protocol::Dash;
        p.video.as_mut().unwrap().source_codec = "h264".into();
        p.audio.as_mut().unwrap().source_codec = "aac".into();
        p.audio.as_mut().unwrap().copy = true;
        let args = ffmpeg_args(&p, Path::Software, &Hardware::software(), 120, std::path::Path::new("/tmp/hls-test")).join(" ");
        assert!(args.contains("-tag:v avc1"), "{args}");
        assert!(args.contains("-tag:a mp4a"), "{args}");
        assert!(args.contains("-ss 600 -noaccurate_seek"), "{args}");
        assert!(args.contains("-f dash"), "{args}");
    }

""" + s[i:]
p.write_text(s)
