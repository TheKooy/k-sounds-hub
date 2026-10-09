package com.kooy.ksoundsoundboard;
import org.json.JSONObject;
import org.json.JSONArray;
public class RemoteReleaseTest {
    static JSONObject asset(long code, String url) throws Exception {
        return new JSONObject().put("name","K-Sounds-Soundboard-Remote-"+code+".apk")
            .put("size",10000).put("state","uploaded").put("browser_download_url",url);
    }
    static void check(boolean ok) { if(!ok) throw new AssertionError(); }
    public static void main(String[] args) throws Exception {
        String repo="TheKooy/k-sounds-hub", prefix="https://github.com/"+repo+"/releases/download/v1/";
        JSONObject release=new JSONObject().put("assets",new JSONArray()
            .put(asset(1,prefix+"K-Sounds-Soundboard-Remote-1.apk"))
            .put(asset(3,prefix+"K-Sounds-Soundboard-Remote-3.apk"))
            .put(asset(2,prefix+"K-Sounds-Soundboard-Remote-2.apk")));
        check(RemoteRelease.newest(release,repo,1).versionCode==3);
        check(RemoteRelease.newest(release,repo,3)==null);
        release.put("prerelease",true);check(RemoteRelease.newest(release,repo,0)==null);
        release.put("prerelease",false);
        for (String url: new String[]{"http://github.com/"+repo+"/releases/download/v1/K-Sounds-Soundboard-Remote-3.apk",
            "https://github.com.evil.test/"+repo+"/releases/download/v1/K-Sounds-Soundboard-Remote-3.apk",
            "https://github.com/other/repo/releases/download/v1/K-Sounds-Soundboard-Remote-3.apk",
            prefix+"../../K-Sounds-Soundboard-Remote-3.apk"}) {
            release.put("assets",new JSONArray().put(asset(3,url)));
            check(RemoteRelease.newest(release,repo,0)==null);
        }
        check(RemoteRelease.repository("https://github.com/"+repo+"/releases/latest").equals(repo));
        try {RemoteRelease.repository("https://evil.test/owner/repo");throw new AssertionError();}
        catch(IllegalArgumentException expected){}
        System.out.println("PASS: release versions, stable channel, HTTPS repository/asset bounds, repository input");
    }
}
