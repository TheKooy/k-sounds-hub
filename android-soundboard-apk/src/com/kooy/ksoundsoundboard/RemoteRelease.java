package com.kooy.ksoundsoundboard;

import java.net.URI;
import java.util.regex.Matcher;
import java.util.regex.Pattern;
import org.json.JSONArray;
import org.json.JSONObject;

/** Public GitHub release metadata. No credentials or pairing token leave the LAN. */
final class RemoteRelease {
    private static final Pattern REPO = Pattern.compile("[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+");
    private static final Pattern APK = Pattern.compile("K-Sounds-Soundboard-Remote-([0-9]{1,10})\\.apk");
    final String name, url;
    final long versionCode, bytes;

    RemoteRelease(String name, String url, long versionCode, long bytes) {
        this.name = name; this.url = url; this.versionCode = versionCode; this.bytes = bytes;
    }

    static String repository(String input) {
        String value = input.trim().replaceFirst("^https://github\\.com/", "")
            .replaceFirst("/releases(?:/latest)?/?$", "").replaceFirst("/$", "");
        if (!REPO.matcher(value).matches() || value.contains("..")) {
            throw new IllegalArgumentException("Enter a GitHub repository such as owner/project or its releases URL.");
        }
        return value;
    }

    static RemoteRelease newest(JSONObject release, String repository, long installedCode) throws Exception {
        if (release.optBoolean("draft") || release.optBoolean("prerelease")) return null;
        JSONArray assets = release.optJSONArray("assets");
        if (assets == null) return null;
        RemoteRelease best = null;
        for (int i = 0; i < assets.length(); i++) {
            JSONObject asset = assets.optJSONObject(i);
            if (asset == null || !"uploaded".equals(asset.optString("state"))) continue;
            String name = asset.optString("name");
            Matcher match = APK.matcher(name);
            if (!match.matches()) continue;
            long code = Long.parseLong(match.group(1)), bytes = asset.optLong("size");
            if (code <= installedCode || code > Integer.MAX_VALUE || bytes < 1 || bytes > 100L * 1024 * 1024) continue;
            String address = asset.optString("browser_download_url");
            URI uri = new URI(address);
            if (!"https".equals(uri.getScheme()) || !"github.com".equalsIgnoreCase(uri.getHost())
                    || uri.getPort() != -1 || uri.getUserInfo() != null || uri.getFragment() != null
                    || uri.getQuery() != null || uri.getPath() == null
                    || !uri.getPath().startsWith("/" + repository + "/releases/download/")
                    || uri.getPath().contains("/../") || !uri.getPath().endsWith("/" + name)) continue;
            if (best == null || code > best.versionCode) best = new RemoteRelease(name, address, code, bytes);
        }
        return best;
    }
}
