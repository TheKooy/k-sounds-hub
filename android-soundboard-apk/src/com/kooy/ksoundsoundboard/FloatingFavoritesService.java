package com.kooy.ksoundsoundboard;

import android.app.Notification;
import android.app.NotificationChannel;
import android.app.NotificationManager;
import android.app.PendingIntent;
import android.app.Service;
import android.content.Context;
import android.content.Intent;
import android.graphics.Color;
import android.graphics.PixelFormat;
import android.graphics.drawable.GradientDrawable;
import android.net.Uri;
import android.os.Build;
import android.os.IBinder;
import android.provider.Settings;
import android.view.Gravity;
import android.view.MotionEvent;
import android.view.View;
import android.view.WindowManager;
import android.webkit.WebSettings;
import android.webkit.WebView;
import android.webkit.WebViewClient;
import android.widget.TextView;

import java.net.URLEncoder;

/** Android overlay owns the bubble; the favorites themselves are rendered by K-Sounds Remote. */
public final class FloatingFavoritesService extends Service {
    public static final String ACTION_START = "com.kooy.ksoundsoundboard.FLOAT_START";
    public static final String ACTION_SHOW = "com.kooy.ksoundsoundboard.FLOAT_SHOW";
    public static final String ACTION_HIDE = "com.kooy.ksoundsoundboard.FLOAT_HIDE";
    public static final String ACTION_STOP = "com.kooy.ksoundsoundboard.FLOAT_STOP";
    private static final String CHANNEL = "ksounds_floating_favorites_no_badge";
    private static final int NOTIFICATION_ID = 1525;
    public static volatile boolean running = false;

    private WindowManager manager;
    private TextView bubble;
    private WindowManager.LayoutParams bubbleParams;
    private WebView favorites;
    private WindowManager.LayoutParams favoritesParams;
    private boolean expanded;

    private int dp(int value) {
        return (int)(value * getResources().getDisplayMetrics().density + .5f);
    }

    @Override public IBinder onBind(Intent intent) { return null; }

    @Override public int onStartCommand(Intent intent, int flags, int startId) {
        if (intent != null && ACTION_STOP.equals(intent.getAction())) {
            stopSelf();
            return START_NOT_STICKY;
        }
        if (intent != null && ACTION_HIDE.equals(intent.getAction())) {
            setOverlayVisible(false);
            return START_NOT_STICKY;
        }
        if (Build.VERSION.SDK_INT >= 23 && !Settings.canDrawOverlays(this)) {
            stopSelf();
            return START_NOT_STICKY;
        }
        String base = getSharedPreferences("ksound_pairing", MODE_PRIVATE).getString("base_url", "");
        String token = getSharedPreferences("ksound_pairing", MODE_PRIVATE).getString("token", "");
        if (base == null || base.isEmpty() || token == null || token.isEmpty()) {
            stopSelf();
            return START_NOT_STICKY;
        }
        startForeground(NOTIFICATION_ID, notification());
        if (bubble == null) showBubble(base, token);
        running = true;
        setOverlayVisible(intent != null && ACTION_SHOW.equals(intent.getAction()));
        return START_NOT_STICKY;
    }

    private Notification notification() {
        NotificationManager nm = (NotificationManager)getSystemService(Context.NOTIFICATION_SERVICE);
        if (Build.VERSION.SDK_INT >= 26) {
            NotificationChannel channel = new NotificationChannel(CHANNEL,
                "K-Sounds floating favorites", NotificationManager.IMPORTANCE_LOW);
            channel.setShowBadge(false); // Do not show a bogus "1" on the launcher icon.
            nm.createNotificationChannel(channel);
        }
        Intent open = new Intent(this, MainActivity.class);
        open.addFlags(Intent.FLAG_ACTIVITY_NEW_TASK | Intent.FLAG_ACTIVITY_SINGLE_TOP);
        Intent stop = new Intent(this, FloatingFavoritesService.class).setAction(ACTION_STOP);
        int pflags = PendingIntent.FLAG_UPDATE_CURRENT;
        if (Build.VERSION.SDK_INT >= 23) pflags |= PendingIntent.FLAG_IMMUTABLE;
        PendingIntent content = PendingIntent.getActivity(this, 24, open, pflags);
        PendingIntent close = PendingIntent.getService(this, 25, stop, pflags);
        Notification.Builder b = Build.VERSION.SDK_INT >= 26
            ? new Notification.Builder(this, CHANNEL) : new Notification.Builder(this);
        return b.setSmallIcon(android.R.drawable.btn_star_big_on)
            .setContentTitle("K-Sounds floating favorites")
            .setContentText("Open the edge handle outside K-Sounds")
            .setOngoing(true)
            .setContentIntent(content)
            .addAction(android.R.drawable.ic_menu_close_clear_cancel, "Stop overlay", close)
            .build();
    }

    private WindowManager.LayoutParams params(int width, int height, int y) {
        int type = Build.VERSION.SDK_INT >= 26
            ? WindowManager.LayoutParams.TYPE_APPLICATION_OVERLAY
            : WindowManager.LayoutParams.TYPE_PHONE;
        WindowManager.LayoutParams p = new WindowManager.LayoutParams(
            dp(width), dp(height), type,
            WindowManager.LayoutParams.FLAG_NOT_FOCUSABLE
                | WindowManager.LayoutParams.FLAG_NOT_TOUCH_MODAL,
            PixelFormat.TRANSLUCENT);
        p.gravity = Gravity.TOP | Gravity.RIGHT;
        p.x = 0; p.y = dp(y);
        return p;
    }

    private void showBubble(final String base, final String token) {
        manager = (WindowManager)getSystemService(WINDOW_SERVICE);
        bubble = new TextView(this);
        bubble.setText("‹"); bubble.setTextSize(25f);
        bubble.setTextColor(Color.rgb(94, 224, 255));
        bubble.setGravity(Gravity.CENTER);
        GradientDrawable background = new GradientDrawable();
        background.setColor(Color.rgb(15, 28, 47));
        background.setStroke(dp(1), Color.rgb(54, 148, 182));
        background.setCornerRadii(new float[]{dp(13),dp(13),0,0,0,0,dp(13),dp(13)});
        bubble.setBackground(background);
        bubbleParams = params(29, 76, 180);
        final int touchSlop = dp(8);
        bubble.setOnTouchListener(new View.OnTouchListener() {
            private float downY;
            private int initialY;
            private boolean dragged;
            @Override public boolean onTouch(View view, MotionEvent e) {
                switch (e.getActionMasked()) {
                    case MotionEvent.ACTION_DOWN:
                        downY = e.getRawY(); initialY = bubbleParams.y; dragged = false;
                        return true;
                    case MotionEvent.ACTION_MOVE:
                        if (Math.abs(e.getRawY() - downY) > touchSlop) dragged = true;
                        if (dragged) {
                            int limit = getResources().getDisplayMetrics().heightPixels - dp(86);
                            bubbleParams.y = Math.max(0,
                                Math.min(limit, initialY + (int)(e.getRawY()-downY)));
                            manager.updateViewLayout(bubble, bubbleParams);
                            if (expanded && favorites != null) {
                                favoritesParams.y = bubbleParams.y;
                                manager.updateViewLayout(favorites, favoritesParams);
                            }
                        }
                        return true;
                    case MotionEvent.ACTION_UP:
                        if (!dragged) togglePanel(base, token);
                        return true;
                    default: return true;
                }
            }
        });
        manager.addView(bubble, bubbleParams);
        bubble.setVisibility(View.GONE);
    }

    private void setOverlayVisible(boolean visible) {
        if (bubble != null) bubble.setVisibility(visible ? View.VISIBLE : View.GONE);
        if (!visible) closePanel();
    }

    private void togglePanel(String base, String token) {
        if (expanded) { closePanel(); return; }
        String url;
        try {
            url = base + "/floating?token=" + URLEncoder.encode(token, "UTF-8");
        } catch (Exception ignored) { return; }
        favorites = new WebView(this);
        favorites.setBackgroundColor(Color.rgb(10, 17, 29));
        WebSettings settings = favorites.getSettings();
        settings.setJavaScriptEnabled(true);
        settings.setDomStorageEnabled(true);
        settings.setCacheMode(WebSettings.LOAD_NO_CACHE);
        final String permitted = base + "/floating?token=";
        favorites.setWebViewClient(new WebViewClient() {
            @Override public boolean shouldOverrideUrlLoading(WebView view, String next) {
                if (next != null && next.equals("ksounds://floating-close")) {
                    closePanel(); return true;
                }
                return next == null || !next.startsWith(permitted);
            }
        });
        int screenWidth = getResources().getDisplayMetrics().widthPixels;
        int screenHeight = getResources().getDisplayMetrics().heightPixels;
        favoritesParams = params(Math.max(160, Math.min(340, (int)((screenWidth / getResources().getDisplayMetrics().density) - 46))),
            Math.min(590, (int)((screenHeight / getResources().getDisplayMetrics().density) - 42)),
            0);
        favoritesParams.x = dp(29);
        favoritesParams.y = bubbleParams.y;
        int bottom = screenHeight - dp(12);
        if (favoritesParams.y + favoritesParams.height > bottom)
            favoritesParams.y = Math.max(dp(10), bottom - favoritesParams.height);
        try {
            manager.addView(favorites, favoritesParams);
            expanded = true;
            if (bubble != null) bubble.setText("›");
            favorites.loadUrl(url);
        } catch (RuntimeException e) {
            favorites.destroy(); favorites = null; expanded = false;
        }
    }

    private void closePanel() {
        if (favorites != null) {
            favorites.stopLoading();
            if (manager != null) manager.removeView(favorites);
            favorites.destroy(); favorites = null;
        }
        expanded = false;
        if (bubble != null) bubble.setText("‹");
    }

    @Override public void onDestroy() {
        closePanel();
        if (bubble != null && manager != null) manager.removeView(bubble);
        bubble = null;
        running = false;
        stopForeground(true);
        super.onDestroy();
    }
}
