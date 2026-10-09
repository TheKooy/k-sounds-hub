package com.kooy.ksoundsoundboard;

import android.app.Activity;
import android.app.AlertDialog;
import android.app.DownloadManager;
import android.content.Context;
import android.content.Intent;
import android.database.Cursor;
import android.os.Build;
import android.provider.Settings;
import android.content.SharedPreferences;
import android.content.pm.PackageInfo;
import android.net.Uri;
import android.os.Environment;
import android.view.View;
import android.widget.EditText;
import android.widget.LinearLayout;
import android.widget.TextView;
import org.json.JSONObject;
import java.io.ByteArrayOutputStream;
import java.io.File;
import java.io.InputStream;
import java.net.HttpURLConnection;
import java.net.URL;

/** Explicit update checks and system downloads; Android owns installation and signature checks. */
final class RemoteUpdates {
    private static final String DEFAULT_REPOSITORY = "TheKooy/k-sounds-hub";
    private static final String LEGACY_DEV_REPOSITORY = "TheKooy/k-sounds-hub-dev";
    private static final int DOWNLOAD_NONE = 0;
    private static final int DOWNLOAD_NEWER = 1;
    private static final int DOWNLOAD_CURRENT = 2;

    private final Activity activity;
    private boolean checking;
    private final SharedPreferences prefs;

    RemoteUpdates(Activity activity) {
        this.activity = activity;
        prefs = activity.getSharedPreferences("remote_updates", Context.MODE_PRIVATE);
        String storedRepository = prefs.getString("repository", "");
        if (LEGACY_DEV_REPOSITORY.equals(storedRepository))
            prefs.edit().putString("repository", DEFAULT_REPOSITORY).apply();
    }

    void show(String displayInfo) {
        LinearLayout panel = new LinearLayout(activity);
        panel.setOrientation(LinearLayout.VERTICAL);
        int pad = (int) (20 * activity.getResources().getDisplayMetrics().density);
        panel.setPadding(pad, pad, pad, pad);
        TextView details = new TextView(activity);
        details.setText(displayInfo + "\n\nInstalled: " + installedVersion()
            + "\n\nUpdates keep your paired PC. Android will ask you to confirm installation.");
        panel.addView(details);
        TextView label = new TextView(activity);
        label.setText("\nGitHub repository"); panel.addView(label);
        EditText source = new EditText(activity);
        source.setSingleLine(true);
        source.setText(prefs.getString("repository", DEFAULT_REPOSITORY));
        panel.addView(source);
        AlertDialog dialog = new AlertDialog.Builder(activity).setTitle("App updates & display")
            .setView(panel).setPositiveButton("Check for updates", null)
            .setNeutralButton("Install download", (d, w) -> installDownload())
            .setNegativeButton("Done", null).create();
        dialog.setOnShowListener(ignored -> {
            int downloadState = downloadState();
            if (downloadState == DOWNLOAD_NONE) {
                dialog.getButton(AlertDialog.BUTTON_NEUTRAL).setVisibility(View.GONE);
            } else if (downloadState == DOWNLOAD_CURRENT) {
                dialog.getButton(AlertDialog.BUTTON_NEUTRAL).setText("Reinstall current version");
            } else {
                dialog.getButton(AlertDialog.BUTTON_NEUTRAL).setText("Install download");
            }
            dialog.getButton(AlertDialog.BUTTON_POSITIVE).setOnClickListener(v -> {
                try {
                    String repo = RemoteRelease.repository(source.getText().toString());
                    prefs.edit().putString("repository", repo).apply();
                    dialog.dismiss(); check(repo);
                } catch (IllegalArgumentException e) { source.setError(e.getMessage()); }
            });
        });
        dialog.show();
    }

    private String installedVersion() {
        try { return activity.getPackageManager().getPackageInfo(activity.getPackageName(), 0).versionName; }
        catch (Exception e) { return "Unknown"; }
    }

    private void check(String repository) {
        if (checking) { message("An update check is already running."); return; }
        checking = true;
        final AlertDialog progress = new AlertDialog.Builder(activity).setTitle("App updates")
            .setMessage("Checking GitHub…").setNegativeButton("Close", null).show();
        new Thread(() -> {
            RemoteRelease release = null;
            String problem = null;
            HttpURLConnection connection = null;
            try {
                URL url = new URL("https://api.github.com/repos/" + repository + "/releases/latest");
                connection = (HttpURLConnection) url.openConnection();
                connection.setInstanceFollowRedirects(false);
                connection.setConnectTimeout(8000); connection.setReadTimeout(8000);
                connection.setRequestProperty("Accept", "application/vnd.github+json");
                connection.setRequestProperty("User-Agent", "K-Sounds-Remote/" + installedVersion());
                connection.setRequestProperty("X-GitHub-Api-Version", "2022-11-28");
                int status = connection.getResponseCode();
                if (status == 404) throw new IllegalStateException("No public release found. Check the repository or try again after an APK is published.");
                if (status == 403 || status == 429) throw new IllegalStateException("GitHub is temporarily limiting requests. Please try again later.");
                if (status != 200) throw new IllegalStateException("GitHub could not be reached (HTTP " + status + ").");
                ByteArrayOutputStream bytes = new ByteArrayOutputStream();
                long deadline = System.nanoTime() + 12000000000L;
                try (InputStream input = connection.getInputStream()) {
                    byte[] buffer = new byte[8192]; int count;
                    while ((count = input.read(buffer)) != -1) {
                        if (bytes.size() + count > 2 * 1024 * 1024 || System.nanoTime() > deadline)
                            throw new IllegalStateException("The release response was too large or too slow.");
                        bytes.write(buffer, 0, count);
                    }
                }
                PackageInfo installed = activity.getPackageManager().getPackageInfo(activity.getPackageName(), 0);
                release = RemoteRelease.newest(new JSONObject(bytes.toString("UTF-8")), repository, installed.versionCode);
            } catch (Exception e) {
                problem = e instanceof IllegalStateException ? e.getMessage() : "Could not check for updates. Check your internet connection and try again.";
            } finally { if (connection != null) connection.disconnect(); }
            final RemoteRelease found = release;
            final String error = problem;
            activity.runOnUiThread(() -> {
                checking = false;
                if (activity.isFinishing() || activity.isDestroyed()) return;
                boolean stillOpen = progress.isShowing();
                progress.dismiss();
                if (!stillOpen) return;
                if (error != null) message(error);
                else if (found == null) message("No newer compatible Remote APK was found in the latest public release.");
                else new AlertDialog.Builder(activity).setTitle("Remote update available")
                    .setMessage("Download the new Remote app (" + Math.max(1, found.bytes / 1024) + " KB)? Your paired PC stays saved.")
                    .setNegativeButton("Later", null)
                    .setPositiveButton("Download update", (d, w) -> download(found)).show();
            });
        }, "ksh-update-check").start();
    }

    private void download(RemoteRelease release) {
        try {
            DownloadManager manager = (DownloadManager) activity.getSystemService(Context.DOWNLOAD_SERVICE);
            if (manager == null) throw new IllegalStateException();
            File directory = activity.getExternalFilesDir(Environment.DIRECTORY_DOWNLOADS);
            if (directory == null) throw new IllegalStateException();
            long previous = prefs.getLong("download_id", -1);
            if (previous >= 0) manager.remove(previous); // Only this app's previous update download.
            String filename = release.versionCode + "-" + System.currentTimeMillis() + ".apk";
            DownloadManager.Request request = new DownloadManager.Request(Uri.parse(release.url))
                .setTitle("K-Sounds Remote update")
                .setDescription("Open when finished and choose Update. Your pairing is kept.")
                .setMimeType("application/vnd.android.package-archive")
                .setNotificationVisibility(DownloadManager.Request.VISIBILITY_VISIBLE_NOTIFY_COMPLETED)
                .setDestinationInExternalFilesDir(activity, Environment.DIRECTORY_DOWNLOADS, filename);
            long id = manager.enqueue(request);
            prefs.edit()
                .putLong("download_id", id)
                .putLong("download_version_code", release.versionCode)
                .apply();
            message("Download started. When it finishes, open App updates & display and tap Install download. Do not uninstall the app; Android checks that the signing key matches.");
        } catch (Exception e) { message("Could not start the download. Check storage and that the Android Downloads app is enabled."); }
    }

    private long installedVersionCode() {
        try { return activity.getPackageManager().getPackageInfo(activity.getPackageName(), 0).versionCode; }
        catch (Exception e) { return Integer.MAX_VALUE; }
    }

    private void clearDownloadReference(long id, DownloadManager manager) {
        if (manager != null && id >= 0) {
            try { manager.remove(id); } catch (Exception ignored) {}
        }
        prefs.edit().remove("download_id").remove("download_version_code").apply();
    }

    private int downloadState() {
        long id = prefs.getLong("download_id", -1);
        long versionCode = prefs.getLong("download_version_code", -1);
        DownloadManager manager = (DownloadManager) activity.getSystemService(Context.DOWNLOAD_SERVICE);
        if (id < 0) return DOWNLOAD_NONE;
        long installed = installedVersionCode();
        if (versionCode < 0 || versionCode < installed) {
            clearDownloadReference(id, manager);
            return DOWNLOAD_NONE;
        }
        if (manager == null) return DOWNLOAD_NONE;
        try (Cursor cursor = manager.query(new DownloadManager.Query().setFilterById(id))) {
            if (cursor == null || !cursor.moveToFirst()) {
                clearDownloadReference(id, manager);
                return DOWNLOAD_NONE;
            }
            int status = cursor.getInt(cursor.getColumnIndexOrThrow(DownloadManager.COLUMN_STATUS));
            if (status == DownloadManager.STATUS_FAILED) {
                clearDownloadReference(id, manager);
                return DOWNLOAD_NONE;
            }
            return versionCode == installed ? DOWNLOAD_CURRENT : DOWNLOAD_NEWER;
        } catch (Exception e) {
            return DOWNLOAD_NONE;
        }
    }

    private void installDownload() {
        try {
            DownloadManager manager = (DownloadManager) activity.getSystemService(Context.DOWNLOAD_SERVICE);
            long id = prefs.getLong("download_id", -1);
            int state = downloadState();
            if (manager == null || id < 0 || state == DOWNLOAD_NONE) {
                message("No downloaded Remote APK is available to install.");
                return;
            }
            try (Cursor cursor = manager.query(new DownloadManager.Query().setFilterById(id))) {
                if (cursor == null || !cursor.moveToFirst()) { message("The download is missing. Check for updates to download it again."); return; }
                int status = cursor.getInt(cursor.getColumnIndexOrThrow(DownloadManager.COLUMN_STATUS));
                if (status == DownloadManager.STATUS_FAILED) { message("The download failed. Check your connection and storage, then download it again."); return; }
                if (status != DownloadManager.STATUS_SUCCESSFUL) { message("The update is still downloading. Try Install download again when it finishes."); return; }
            }
            if (Build.VERSION.SDK_INT >= 26 && !activity.getPackageManager().canRequestPackageInstalls()) {
                new AlertDialog.Builder(activity).setTitle("Allow this update")
                    .setMessage("Enable Allow from this source for K-Sounds, then return and tap Install download. Android will still ask you to confirm the update.")
                    .setNegativeButton("Later", null)
                    .setPositiveButton("Open settings", (d, w) -> {
                        try { activity.startActivity(new Intent(Settings.ACTION_MANAGE_UNKNOWN_APP_SOURCES,
                            Uri.parse("package:" + activity.getPackageName()))); }
                        catch (Exception e) { message("Open Android Settings to allow app updates from K-Sounds."); }
                    }).show();
                return;
            }
            Uri file = manager.getUriForDownloadedFile(id);
            if (file == null) { message("The download could not be opened. Download it again."); return; }
            activity.startActivity(new Intent(Intent.ACTION_VIEW)
                .setDataAndType(file, "application/vnd.android.package-archive")
                .addFlags(Intent.FLAG_GRANT_READ_URI_PERMISSION));
        } catch (Exception e) { message("Android could not open the installer. Open the completed download from its notification."); }
    }

    private void message(String text) {
        if (!activity.isFinishing() && !activity.isDestroyed())
            new AlertDialog.Builder(activity).setTitle("App updates").setMessage(text).setPositiveButton("OK", null).show();
    }
}
