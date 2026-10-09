package com.kooy.ksoundsoundboard;

import android.app.Activity;
import android.content.Intent;
import android.net.Uri;
import android.os.Build;
import android.provider.Settings;
import android.content.Context;
import android.content.SharedPreferences;
import android.graphics.Color;
import android.graphics.Typeface;
import android.graphics.drawable.GradientDrawable;
import android.hardware.display.DisplayManager;
import android.net.DhcpInfo;
import android.net.wifi.WifiManager;
import android.os.Bundle;
import android.text.InputFilter;
import android.text.InputType;
import android.view.Gravity;
import android.view.Display;
import android.view.View;
import android.view.Window;
import android.view.WindowManager;
import android.view.inputmethod.EditorInfo;
import android.view.inputmethod.InputMethodManager;
import android.webkit.WebResourceError;
import android.webkit.WebResourceRequest;
import android.webkit.WebSettings;
import android.webkit.WebView;
import android.webkit.WebViewClient;
import android.widget.Button;
import android.widget.EditText;
import android.widget.LinearLayout;
import android.widget.TextView;

import org.json.JSONObject;

import java.io.BufferedReader;
import java.io.InputStreamReader;
import java.net.DatagramPacket;
import java.net.DatagramSocket;
import java.net.HttpURLConnection;
import java.net.InetAddress;
import java.net.URL;
import java.net.URLEncoder;
import java.util.ArrayList;
import java.util.LinkedHashSet;
import java.util.List;
import java.util.Set;

public class MainActivity extends Activity {
    private static final int DISCOVERY_PORT = 8766;
    private static final String DISCOVERY_REQUEST = "KSH_DISCOVER_V2";
    private static final String EXPECTED_SERVICE = "KSH_SOUNDBOARD";

    private static final int BG = Color.rgb(7, 10, 18);
    private static final int CARD = Color.rgb(12, 18, 30);
    private static final int PANEL = Color.rgb(8, 13, 24);
    private static final int CYAN = Color.rgb(62, 216, 255);
    private static final int MAGENTA = Color.rgb(255, 92, 199);
    private static final int TEXT = Color.rgb(236, 247, 255);
    private static final int MUTED = Color.rgb(147, 164, 184);
    private static final int DANGER = Color.rgb(255, 111, 132);

    private SharedPreferences prefs;
    private WebView webView;
    private LinearLayout root;
    private TextView status;
    private EditText serverInput;
    private EditText pinInput;
    private Button primaryButton;
    private Button secondaryButton;
    private Button tertiaryButton;

    private String discoveredBaseUrl = "";
    private boolean awaitingOverlayPermission;
    private static final String PREF_EDGE_AUTO = "floating_auto_show";
    private String activeWebBaseUrl = "";
    private String activeWebToken = "";
    private volatile String lastDiscoveryError = "";
    private boolean webLoadFailureHandled = false;
    private DisplayManager displayManager;
    private RemoteUpdates remoteUpdates;
    private boolean displayForeground;
    private float requestedDisplayHz;

    private void requestViewRefresh(View view, float rate) {
        if (view == null || android.os.Build.VERSION.SDK_INT < 35) return;
        // Public ARR API; reflection keeps builds compatible with older SDK platforms.
        try { View.class.getMethod("setRequestedFrameRate", float.class).invoke(view, rate); }
        catch (ReflectiveOperationException ignored) { /* Older Android 15 builds. */ }
    }

    private String displayInfo() {
        Display display = getWindowManager().getDefaultDisplay();
        return String.format(java.util.Locale.US,
            "Refresh: adaptive maximum\nDisplay now: %.0f Hz · Requested maximum: %.0f Hz\nAndroid may lower the rate while idle, on battery saver or when hot.",
            display == null ? 0 : display.getRefreshRate(), requestedDisplayHz);
    }

    private final DisplayManager.DisplayListener displayListener = new DisplayManager.DisplayListener() {
        @Override public void onDisplayAdded(int displayId) { applyDisplayRefreshPreference(); }
        @Override public void onDisplayRemoved(int displayId) { applyDisplayRefreshPreference(); }
        @Override public void onDisplayChanged(int displayId) { applyDisplayRefreshPreference(); }
    };

    private void applyDisplayRefreshPreference() {
        if (!displayForeground) return;
        Display display = getWindowManager().getDefaultDisplay();
        if (display == null) return;
        Display.Mode current = display.getMode();
        float maximum = current.getRefreshRate();
        for (Display.Mode mode : display.getSupportedModes()) {
            if (mode.getPhysicalWidth() == current.getPhysicalWidth()
                    && mode.getPhysicalHeight() == current.getPhysicalHeight()) {
                maximum = Math.max(maximum, mode.getRefreshRate());
            }
        }
        requestedDisplayHz = maximum;
        requestViewRefresh(getWindow().getDecorView(), maximum);
        requestViewRefresh(webView, maximum);
        WindowManager.LayoutParams params = getWindow().getAttributes();
        // A preference lets Android adapt for idle, power and thermal conditions.
        // Do not pin a display mode or a minimum frequency.
        if (Math.abs(params.preferredRefreshRate - maximum) > 0.1f) {
            params.preferredRefreshRate = maximum;
            getWindow().setAttributes(params);
        }
    }

    @Override
    protected void onCreate(Bundle savedInstanceState) {
        super.onCreate(savedInstanceState);

        requestWindowFeature(Window.FEATURE_NO_TITLE);
        getWindow().setFlags(
            WindowManager.LayoutParams.FLAG_FULLSCREEN,
            WindowManager.LayoutParams.FLAG_FULLSCREEN
        );
        getWindow().addFlags(WindowManager.LayoutParams.FLAG_KEEP_SCREEN_ON);

        prefs = getSharedPreferences("ksound_pairing", MODE_PRIVATE);
        remoteUpdates = new RemoteUpdates(this);
        immersive();

        String token = prefs.getString("token", "");
        String baseUrl = prefs.getString("base_url", "");

        if (!token.isEmpty() && !baseUrl.isEmpty()) {
            showConnectingUi(baseUrl, token);
            retrySavedConnection(baseUrl, token);
        } else {
            showPairingUi("Searching for K-Sounds Hub on your network...");
            discoverAndShow();
        }
    }

    private int dp(int value) {
        return (int) (value * getResources().getDisplayMetrics().density + 0.5f);
    }

    private GradientDrawable rounded(int color, int strokeColor, int strokeDp, int radiusDp) {
        GradientDrawable drawable = new GradientDrawable();
        drawable.setColor(color);
        drawable.setCornerRadius(dp(radiusDp));
        if (strokeDp > 0) {
            drawable.setStroke(dp(strokeDp), strokeColor);
        }
        return drawable;
    }

    private TextView makeText(String text, int sizeSp, int color, int style) {
        TextView view = new TextView(this);
        view.setText(text);
        view.setTextColor(color);
        view.setTextSize(sizeSp);
        view.setTypeface(Typeface.DEFAULT, style);
        view.setGravity(Gravity.CENTER);
        return view;
    }

    private Button makeButton(String text, int role) {
        Button button = new Button(this);
        button.setText(text);
        button.setAllCaps(false);
        button.setTextSize(14);
        button.setTypeface(Typeface.DEFAULT, Typeface.BOLD);
        button.setPadding(dp(14), dp(8), dp(14), dp(8));

        int fill = Color.rgb(18, 24, 38);
        int stroke = CYAN;
        int textColor = TEXT;

        if (role == 1) {
            fill = CYAN;
            stroke = CYAN;
            textColor = Color.rgb(7, 16, 24);
        } else if (role == 2) {
            fill = Color.rgb(36, 14, 32);
            stroke = MAGENTA;
            textColor = TEXT;
        } else if (role == 3) {
            fill = Color.rgb(34, 17, 26);
            stroke = DANGER;
            textColor = Color.rgb(255, 225, 231);
        }

        button.setTextColor(textColor);
        button.setBackground(rounded(fill, stroke, 1, 14));
        return button;
    }

    private View spacer(int heightDp) {
        View view = new View(this);
        view.setLayoutParams(new LinearLayout.LayoutParams(1, dp(heightDp)));
        return view;
    }

    private LinearLayout makeRoot() {
        webView = null;

        root = new LinearLayout(this);
        root.setOrientation(LinearLayout.VERTICAL);
        root.setGravity(Gravity.CENTER);
        root.setPadding(dp(14), dp(14), dp(14), dp(14));
        root.setBackgroundColor(BG);

        setContentView(root);
        immersive();
        return root;
    }

    private LinearLayout makeCard(String title, String subtitle, String message) {
        int screenWidth = getResources().getDisplayMetrics().widthPixels;
        boolean narrow = screenWidth < dp(620);

        LinearLayout card = new LinearLayout(this);
        card.setOrientation(LinearLayout.VERTICAL);
        card.setGravity(Gravity.CENTER);
        card.setPadding(dp(24), dp(22), dp(24), dp(22));
        card.setBackground(rounded(CARD, CYAN, 1, 24));

        int cardWidth = Math.min(screenWidth - dp(28), dp(760));
        if (cardWidth < dp(280)) {
            cardWidth = screenWidth - dp(16);
        }

        root.addView(card, new LinearLayout.LayoutParams(cardWidth, LinearLayout.LayoutParams.WRAP_CONTENT));
        Button appOptions = makeButton("App updates & display", 0);
        appOptions.setOnClickListener(v -> remoteUpdates.show(displayInfo()));
        root.addView(appOptions);

        TextView badge = makeText("K", 28, CYAN, Typeface.BOLD);
        badge.setBackground(rounded(Color.rgb(5, 8, 15), MAGENTA, 1, 18));
        badge.setPadding(dp(16), dp(8), dp(16), dp(8));
        LinearLayout.LayoutParams badgeParams = new LinearLayout.LayoutParams(
            LinearLayout.LayoutParams.WRAP_CONTENT,
            LinearLayout.LayoutParams.WRAP_CONTENT
        );
        badgeParams.setMargins(0, 0, 0, dp(12));
        card.addView(badge, badgeParams);

        TextView titleView = makeText(title, narrow ? 22 : 28, TEXT, Typeface.BOLD);
        titleView.setLetterSpacing(0.08f);
        card.addView(titleView);

        TextView subtitleView = makeText(subtitle, narrow ? 12 : 14, MUTED, Typeface.NORMAL);
        subtitleView.setPadding(0, dp(6), 0, dp(16));
        card.addView(subtitleView);

        status = makeText(message, narrow ? 13 : 15, CYAN, Typeface.NORMAL);
        status.setPadding(dp(12), dp(10), dp(12), dp(10));
        status.setBackground(rounded(PANEL, Color.rgb(38, 88, 112), 1, 16));
        LinearLayout.LayoutParams statusParams = new LinearLayout.LayoutParams(
            LinearLayout.LayoutParams.MATCH_PARENT,
            LinearLayout.LayoutParams.WRAP_CONTENT
        );
        statusParams.setMargins(0, 0, 0, dp(16));
        card.addView(status, statusParams);

        return card;
    }

    private void addButtonRow(LinearLayout card, Button first, Button second, Button third) {
        int screenWidth = getResources().getDisplayMetrics().widthPixels;
        boolean narrow = screenWidth < dp(620);

        LinearLayout row = new LinearLayout(this);
        row.setOrientation(narrow ? LinearLayout.VERTICAL : LinearLayout.HORIZONTAL);
        row.setGravity(Gravity.CENTER);

        LinearLayout.LayoutParams buttonParams = new LinearLayout.LayoutParams(
            narrow ? LinearLayout.LayoutParams.MATCH_PARENT : dp(180),
            dp(48)
        );
        buttonParams.setMargins(dp(6), dp(4), dp(6), dp(4));

        if (first != null) {
            row.addView(first, buttonParams);
        }
        if (second != null) {
            row.addView(second, buttonParams);
        }
        if (third != null) {
            row.addView(third, buttonParams);
        }

        card.addView(row);
    }

    private void showPairingUi(String message) {
        makeRoot();

        LinearLayout card = makeCard(
            "K-SOUNDS",
            "Soundboard Remote",
            message
        );

        int screenWidth = getResources().getDisplayMetrics().widthPixels;
        boolean narrow = screenWidth < dp(620);

        serverInput = new EditText(this);
        serverInput.setHint("PC local IP address or host name");
        serverInput.setSingleLine(true);
        serverInput.setInputType(InputType.TYPE_CLASS_TEXT | InputType.TYPE_TEXT_VARIATION_URI);
        serverInput.setImeOptions(EditorInfo.IME_ACTION_NEXT | EditorInfo.IME_FLAG_NO_EXTRACT_UI);
        serverInput.setTextColor(Color.WHITE);
        serverInput.setHintTextColor(MUTED);
        serverInput.setGravity(Gravity.CENTER);
        serverInput.setTextSize(narrow ? 15 : 17);
        serverInput.setPadding(dp(14), dp(10), dp(14), dp(10));
        serverInput.setBackground(rounded(Color.rgb(5, 8, 15), CYAN, 1, 16));

        String savedBaseUrl = prefs.getString("base_url", "");
        if (savedBaseUrl != null && !savedBaseUrl.isEmpty()) {
            serverInput.setText(savedBaseUrl);
        }

        LinearLayout.LayoutParams serverParams = new LinearLayout.LayoutParams(
            narrow ? LinearLayout.LayoutParams.MATCH_PARENT : dp(360),
            LinearLayout.LayoutParams.WRAP_CONTENT
        );
        serverParams.setMargins(0, 0, 0, dp(10));
        card.addView(serverInput, serverParams);

        pinInput = new EditText(this);
        pinInput.setHint("6-digit PC code");
        pinInput.setSingleLine(true);
        pinInput.setInputType(InputType.TYPE_CLASS_NUMBER | InputType.TYPE_NUMBER_VARIATION_NORMAL);
        pinInput.setImeOptions(EditorInfo.IME_ACTION_DONE | EditorInfo.IME_FLAG_NO_EXTRACT_UI);
        pinInput.setPrivateImeOptions("com.google.android.inputmethod.latin.noFullscreenKeyboard=true");
        pinInput.setFilters(new InputFilter[] { new InputFilter.LengthFilter(6) });
        pinInput.setSelectAllOnFocus(true);
        pinInput.setTextColor(Color.WHITE);
        pinInput.setHintTextColor(MUTED);
        pinInput.setGravity(Gravity.CENTER);
        pinInput.setTextSize(narrow ? 20 : 22);
        pinInput.setTypeface(Typeface.DEFAULT, Typeface.BOLD);
        pinInput.setPadding(dp(14), dp(10), dp(14), dp(10));
        pinInput.setBackground(rounded(Color.rgb(5, 8, 15), MAGENTA, 1, 16));

        LinearLayout.LayoutParams pinParams = new LinearLayout.LayoutParams(
            narrow ? LinearLayout.LayoutParams.MATCH_PARENT : dp(280),
            LinearLayout.LayoutParams.WRAP_CONTENT
        );
        pinParams.setMargins(0, 0, 0, dp(14));
        card.addView(pinInput, pinParams);

        primaryButton = makeButton("Connect", 1);
        primaryButton.setOnClickListener(new View.OnClickListener() {
            @Override
            public void onClick(View v) {
                pairWithPin();
            }
        });

        secondaryButton = makeButton("Search PC", 0);
        secondaryButton.setOnClickListener(new View.OnClickListener() {
            @Override
            public void onClick(View v) {
                discoverAndShow();
            }
        });

        tertiaryButton = makeButton("Use address", 0);
        tertiaryButton.setOnClickListener(new View.OnClickListener() {
            @Override
            public void onClick(View v) {
                useManualAddress();
            }
        });

        addButtonRow(card, primaryButton, secondaryButton, tertiaryButton);

        TextView help = makeText(
            "On the PC: open K-Sounds Hub and click Pair Android, then enter the 6-digit code here.",
            narrow ? 11 : 12,
            MUTED,
            Typeface.NORMAL
        );
        help.setPadding(0, dp(14), 0, 0);
        card.addView(help);

        setPairControlsEnabled(false);
    }

    private void showConnectingUi(final String baseUrl, final String token) {
        makeRoot();

        LinearLayout card = makeCard(
            "K-SOUNDS",
            "Remote",
            "Connecting to saved PC...\n" + baseUrl
        );

        primaryButton = makeButton("Retry", 1);
        primaryButton.setOnClickListener(new View.OnClickListener() {
            @Override
            public void onClick(View v) {
                retrySavedConnection(baseUrl, token);
            }
        });

        secondaryButton = makeButton("Pair again", 2);
        secondaryButton.setOnClickListener(new View.OnClickListener() {
            @Override
            public void onClick(View v) {
                startPairingWithoutForgetting();
            }
        });

        tertiaryButton = makeButton("Disconnect", 3);
        tertiaryButton.setOnClickListener(new View.OnClickListener() {
            @Override
            public void onClick(View v) {
                forgetSavedPc();
            }
        });

        addButtonRow(card, primaryButton, secondaryButton, tertiaryButton);
    }

    private void showServerNotFoundUi(final String baseUrl, final String token, final String reason) {
        runOnUiThread(new Runnable() {
            @Override
            public void run() {
                makeRoot();

                LinearLayout card = makeCard(
                    "SERVER NOT FOUND",
                    "Remote — Server not found",
                    reason
                    + "\n\nSaved PC: "
                    + (baseUrl == null || baseUrl.length() == 0 ? "none" : baseUrl)
                    + "\n\nCheck that K-Sounds Hub is running, Pair Android is active, and the phone is on the same LAN."
                );

                primaryButton = makeButton("Retry", 1);
                primaryButton.setOnClickListener(new View.OnClickListener() {
                    @Override
                    public void onClick(View v) {
                        retrySavedConnection(baseUrl, token);
                    }
                });

                secondaryButton = makeButton("Pair again", 2);
                secondaryButton.setOnClickListener(new View.OnClickListener() {
                    @Override
                    public void onClick(View v) {
                        startPairingWithoutForgetting();
                    }
                });

                tertiaryButton = makeButton("Disconnect", 3);
                tertiaryButton.setOnClickListener(new View.OnClickListener() {
                    @Override
                    public void onClick(View v) {
                        forgetSavedPc();
                    }
                });

                addButtonRow(card, primaryButton, secondaryButton, tertiaryButton);
            }
        });
    }

    private JSONObject tryParseJson(String text) {
        try {
            return new JSONObject(text);
        } catch (Exception e) {
            return null;
        }
    }

    private void setPairControlsEnabled(final boolean enabled) {
        runOnUiThread(new Runnable() {
            @Override
            public void run() {
                if (primaryButton != null) {
                    primaryButton.setEnabled(enabled);
                    primaryButton.setAlpha(enabled ? 1.0f : 0.45f);
                }
            }
        });
    }

    private void setSearching(final boolean searching) {
        runOnUiThread(new Runnable() {
            @Override
            public void run() {
                if (secondaryButton != null) {
                    secondaryButton.setText(searching ? "Searching..." : "Search PC");
                    secondaryButton.setEnabled(!searching);
                    secondaryButton.setAlpha(searching ? 0.55f : 1.0f);
                }
            }
        });
    }

    private void focusPinInput() {
        runOnUiThread(new Runnable() {
            @Override
            public void run() {
                if (pinInput == null) {
                    return;
                }

                pinInput.requestFocus();
                pinInput.postDelayed(new Runnable() {
                    @Override
                    public void run() {
                        InputMethodManager imm = (InputMethodManager) getSystemService(Context.INPUT_METHOD_SERVICE);
                        if (imm != null) {
                            imm.showSoftInput(pinInput, InputMethodManager.SHOW_IMPLICIT);
                        }
                    }
                }, 180);
            }
        });
    }

    private void setStatus(final String text) {
        runOnUiThread(new Runnable() {
            @Override
            public void run() {
                if (status != null) {
                    status.setText(text);
                }
            }
        });
    }

    private void clearSavedPairingAndRestart(boolean autoSearch) {
        prefs.edit()
            .remove("base_url")
            .remove("token")
            .apply();

        discoveredBaseUrl = "";
        activeWebBaseUrl = "";
        activeWebToken = "";
        webLoadFailureHandled = false;

        showPairingUi(autoSearch
            ? "Searching for K-Sounds Hub on your network..."
            : "Saved PC forgotten. Tap Search PC when you are ready."
        );

        if (autoSearch) {
            discoverAndShow();
        }
    }

    private void startPairingWithoutForgetting() {
        discoveredBaseUrl = "";
        activeWebBaseUrl = "";
        activeWebToken = "";
        webLoadFailureHandled = false;

        showPairingUi("Searching for K-Sounds Hub on your network...");
        discoverAndShow();
    }

    private void forgetSavedPc() {
        clearSavedPairingAndRestart(false);
    }

    private void retrySavedConnection(final String fallbackBaseUrl, final String token) {
        setStatus("Searching for K-Sounds Hub...");
        if (primaryButton != null) {
            primaryButton.setEnabled(false);
            primaryButton.setAlpha(0.55f);
        }

        new Thread(new Runnable() {
            @Override
            public void run() {
                String base = discoverServer();
                if (base == null || base.isEmpty()) {
                    base = fallbackBaseUrl;
                }

                final String targetBase = base;

                runOnUiThread(new Runnable() {
                    @Override
                    public void run() {
                        if (primaryButton != null) {
                            primaryButton.setEnabled(true);
                            primaryButton.setAlpha(1.0f);
                        }
                    }
                });

                if (targetBase == null || targetBase.isEmpty()) {
                    showServerNotFoundUi(
                        fallbackBaseUrl,
                        token,
                        "K-Sounds Hub could not be found on this network."
                    );
                    return;
                }

                prefs.edit().putString("base_url", targetBase).apply();

                runOnUiThread(new Runnable() {
                    @Override
                    public void run() {
                        showWeb(targetBase, token);
                    }
                });
            }
        }).start();
    }

    private void handleWebLoadFailure(String reason) {
        if (webLoadFailureHandled) {
            return;
        }

        webLoadFailureHandled = true;

        if (reason == null || reason.trim().length() == 0) {
            reason = "The remote page did not load.";
        }

        showServerNotFoundUi(activeWebBaseUrl, activeWebToken, reason);
    }

    private void discoverAndShow() {
        setPairControlsEnabled(false);
        setSearching(true);
        setStatus("Searching for K-Sounds Hub on your LAN...");

        new Thread(new Runnable() {
            @Override
            public void run() {
                String base = discoverServer();
                setSearching(false);

                if (base == null || base.isEmpty()) {
                    discoveredBaseUrl = "";
                    String detail = lastDiscoveryError == null || lastDiscoveryError.isEmpty()
                        ? "No discovery response received."
                        : lastDiscoveryError;
                    setStatus(
                        "PC not found. " + detail
                        + "\nCheck the active PC firewall zone, or enter the PC address manually."
                    );
                    return;
                }

                discoveredBaseUrl = base;
                setPairControlsEnabled(true);
                setStatus("PC found: " + base + "\nEnter the 6-digit code shown by K-Sounds Hub → Pair Android.");
                focusPinInput();
            }
        }).start();
    }

    private String normalizeBaseUrl(String rawValue) {
        String value = rawValue == null ? "" : rawValue.trim();
        if (value.isEmpty()) {
            return "";
        }

        if (!value.startsWith("http://") && !value.startsWith("https://")) {
            value = "http://" + value;
        }

        try {
            URL parsed = new URL(value);
            String host = parsed.getHost();
            if (host == null || host.isEmpty()) {
                return "";
            }
            int port = parsed.getPort() > 0 ? parsed.getPort() : 8765;
            return parsed.getProtocol() + "://" + host + ":" + port;
        } catch (Exception e) {
            return "";
        }
    }

    private boolean probeBaseUrl(String baseUrl) {
        HttpURLConnection conn = null;
        try {
            URL url = new URL(baseUrl + "/health");
            conn = (HttpURLConnection) url.openConnection();
            conn.setConnectTimeout(1800);
            conn.setReadTimeout(1800);
            conn.setUseCaches(false);
            conn.setRequestMethod("GET");

            int code = conn.getResponseCode();
            if (code != 200) {
                lastDiscoveryError = "HTTP health check returned " + code + ".";
                return false;
            }

            BufferedReader reader = new BufferedReader(new InputStreamReader(conn.getInputStream()));
            StringBuilder body = new StringBuilder();
            String line;
            while ((line = reader.readLine()) != null) {
                body.append(line);
            }
            reader.close();

            JSONObject json = tryParseJson(body.toString());
            boolean healthy = json != null
                && json.optBoolean("ok", false)
                && "k-sounds-soundboard-remote".equals(json.optString("service", ""));
            if (!healthy) {
                lastDiscoveryError = "The address answered, but it is not K-Sounds Soundboard Remote.";
            }
            return healthy;
        } catch (Exception e) {
            lastDiscoveryError = "Direct connection failed: " + e.getClass().getSimpleName()
                + (e.getMessage() == null ? "" : " — " + e.getMessage());
            return false;
        } finally {
            if (conn != null) {
                conn.disconnect();
            }
        }
    }

    private void useManualAddress() {
        final String raw = serverInput == null ? "" : serverInput.getText().toString();
        final String baseUrl = normalizeBaseUrl(raw);
        if (baseUrl.isEmpty()) {
            setStatus("Enter a valid PC local IP address or host name.");
            return;
        }

        setStatus("Checking " + baseUrl + "...");
        setPairControlsEnabled(false);
        if (tertiaryButton != null) {
            tertiaryButton.setEnabled(false);
        }

        new Thread(new Runnable() {
            @Override
            public void run() {
                final boolean healthy = probeBaseUrl(baseUrl);
                runOnUiThread(new Runnable() {
                    @Override
                    public void run() {
                        if (tertiaryButton != null) {
                            tertiaryButton.setEnabled(true);
                        }
                        if (!healthy) {
                            setStatus(lastDiscoveryError);
                            setPairControlsEnabled(false);
                            return;
                        }

                        discoveredBaseUrl = baseUrl;
                        prefs.edit().putString("base_url", baseUrl).apply();
                        setPairControlsEnabled(true);
                        setStatus("PC found: " + baseUrl + "\nEnter the 6-digit pairing code.");
                        focusPinInput();
                    }
                });
            }
        }).start();
    }

    private List<InetAddress> discoveryTargets(WifiManager wifiManager) {
        Set<String> addresses = new LinkedHashSet<String>();
        addresses.add("255.255.255.255");

        try {
            DhcpInfo dhcp = wifiManager == null ? null : wifiManager.getDhcpInfo();
            if (dhcp != null && dhcp.ipAddress != 0 && dhcp.netmask != 0) {
                int broadcast = (dhcp.ipAddress & dhcp.netmask) | ~dhcp.netmask;
                byte[] bytes = new byte[] {
                    (byte) (broadcast & 0xff),
                    (byte) ((broadcast >> 8) & 0xff),
                    (byte) ((broadcast >> 16) & 0xff),
                    (byte) ((broadcast >> 24) & 0xff)
                };
                addresses.add(InetAddress.getByAddress(bytes).getHostAddress());
            }
        } catch (Exception ignored) {
        }

        List<InetAddress> result = new ArrayList<InetAddress>();
        for (String address : addresses) {
            try {
                result.add(InetAddress.getByName(address));
            } catch (Exception ignored) {
            }
        }
        return result;
    }

    private String discoverServer() {
        DatagramSocket socket = null;
        WifiManager.MulticastLock multicastLock = null;
        lastDiscoveryError = "";
        try {
            WifiManager wifiManager = (WifiManager) getApplicationContext().getSystemService(Context.WIFI_SERVICE);
            if (wifiManager != null) {
                multicastLock = wifiManager.createMulticastLock("ksounds-remote-discovery");
                multicastLock.setReferenceCounted(false);
                multicastLock.acquire();
            }

            socket = new DatagramSocket();
            socket.setBroadcast(true);
            socket.setSoTimeout(850);

            byte[] out = DISCOVERY_REQUEST.getBytes("UTF-8");
            List<InetAddress> targets = discoveryTargets(wifiManager);
            for (int attempt = 0; attempt < 3; attempt++) {
                for (InetAddress target : targets) {
                    DatagramPacket packet = new DatagramPacket(
                        out,
                        out.length,
                        target,
                        DISCOVERY_PORT
                    );
                    socket.send(packet);
                }

                long deadline = System.currentTimeMillis() + 850;
                while (System.currentTimeMillis() < deadline) {
                    try {
                        byte[] buf = new byte[2048];
                        DatagramPacket response = new DatagramPacket(buf, buf.length);
                        socket.receive(response);

                        String body = new String(response.getData(), 0, response.getLength(), "UTF-8");
                        JSONObject json = new JSONObject(body);
                        if (!EXPECTED_SERVICE.equals(json.optString("service", ""))) {
                            continue;
                        }

                        int port = json.optInt("http_port", 8765);
                        String host = response.getAddress().getHostAddress();
                        String baseUrl = "http://" + host + ":" + port;
                        if (probeBaseUrl(baseUrl)) {
                            return baseUrl;
                        }
                    } catch (java.net.SocketTimeoutException timeout) {
                        break;
                    }
                }
            }

            String savedBaseUrl = normalizeBaseUrl(prefs.getString("base_url", ""));
            String savedAddressError = "";
            if (!savedBaseUrl.isEmpty()) {
                if (probeBaseUrl(savedBaseUrl)) {
                    return savedBaseUrl;
                }
                savedAddressError = lastDiscoveryError;
            }

            lastDiscoveryError = "No UDP response on port 8766 after 3 attempts."
                + (savedAddressError.isEmpty() ? "" : " Saved address: " + savedAddressError);
            return "";
        } catch (Exception e) {
            lastDiscoveryError = "Discovery failed: " + e.getClass().getSimpleName()
                + (e.getMessage() == null ? "" : " — " + e.getMessage());
            return "";
        } finally {
            if (socket != null) {
                socket.close();
            }
            if (multicastLock != null && multicastLock.isHeld()) {
                multicastLock.release();
            }
        }
    }

    private void pairWithPin() {
        if (pinInput == null) {
            return;
        }

        final String pin = pinInput.getText().toString().trim();

        if (pin.length() == 0) {
            setStatus("Enter the 6-digit code shown by K-Sounds Hub → Pair Android.");
            focusPinInput();
            return;
        }

        if (!pin.matches("[0-9]+")) {
            setStatus("The pairing code must contain digits only.");
            focusPinInput();
            return;
        }

        if (discoveredBaseUrl == null || discoveredBaseUrl.isEmpty()) {
            setStatus("PC not found yet. Tap Search PC.");
            return;
        }

        setStatus("Pairing with PC...");
        setPairControlsEnabled(false);

        new Thread(new Runnable() {
            @Override
            public void run() {
                try {
                    String encodedPin = URLEncoder.encode(pin, "UTF-8");
                    URL url = new URL(discoveredBaseUrl + "/api/pair?pin=" + encodedPin);
                    HttpURLConnection conn = (HttpURLConnection) url.openConnection();
                    conn.setConnectTimeout(2500);
                    conn.setReadTimeout(2500);
                    conn.setRequestMethod("GET");

                    int code = conn.getResponseCode();
                    BufferedReader reader = new BufferedReader(new InputStreamReader(
                        code >= 200 && code < 300 ? conn.getInputStream() : conn.getErrorStream()
                    ));

                    StringBuilder sb = new StringBuilder();
                    String line;
                    while ((line = reader.readLine()) != null) {
                        sb.append(line);
                    }

                    String responseText = sb.toString();
                    JSONObject json = tryParseJson(responseText);

                    if (code >= 200 && code < 300 && json != null && json.optBoolean("ok", false)) {
                        String token = json.optString("token", "");
                        if (token.isEmpty()) {
                            setStatus("Pairing succeeded, but the token is empty.");
                            setPairControlsEnabled(true);
                            return;
                        }

                        prefs.edit()
                            .putString("base_url", discoveredBaseUrl)
                            .putString("token", token)
                            .apply();

                        runOnUiThread(new Runnable() {
                            @Override
                            public void run() {
                                showWeb(discoveredBaseUrl, prefs.getString("token", ""));
                            }
                        });
                    } else {
                        String msg;
                        if (json != null) {
                            msg = json.optString("error", "Pairing refused.");
                        } else if (responseText != null && responseText.trim().length() > 0) {
                            msg = responseText.trim();
                        } else {
                            msg = "HTTP " + code;
                        }

                        setStatus("Pairing failed: " + msg);
                        setPairControlsEnabled(true);
                    }
                } catch (Exception e) {
                    setStatus("Pairing error: " + e.getMessage());
                    setPairControlsEnabled(true);
                }
            }
        }).start();
    }

    private boolean handleSpecialUrl(String url) {
        if (url == null) {
            return false;
        }

        if (url.equals("ksounds://floating-auto-on") || url.equals("ksounds://floating-auto-off")) {
            boolean enabled = url.endsWith("-on");
            prefs.edit().putBoolean(PREF_EDGE_AUTO, enabled).apply();
            if (enabled && Build.VERSION.SDK_INT >= 23 && !Settings.canDrawOverlays(this)) {
                awaitingOverlayPermission = true;
                startActivity(new Intent(Settings.ACTION_MANAGE_OVERLAY_PERMISSION,
                    Uri.parse("package:" + getPackageName())));
            } else if (!enabled && FloatingFavoritesService.running) {
                startService(new Intent(this, FloatingFavoritesService.class)
                    .setAction(FloatingFavoritesService.ACTION_STOP));
            }
            return true;
        }
        if (url.equals("ksounds://floating-toggle")) {
            toggleFloatingFavorites();
            return true;
        }
        if (url.equals("ksounds://app-options")) {
            remoteUpdates.show(displayInfo());
            return true;
        }
        if (url.startsWith("ksounds://disconnect")) {
            forgetSavedPc();
            return true;
        }

        return false;
    }

    private void toggleFloatingFavorites() {
        if (FloatingFavoritesService.running) {
            startService(new Intent(this, FloatingFavoritesService.class)
                .setAction(FloatingFavoritesService.ACTION_STOP));
            return;
        }
        if (Build.VERSION.SDK_INT >= 23 && !Settings.canDrawOverlays(this)) {
            awaitingOverlayPermission = true;
            Intent request = new Intent(Settings.ACTION_MANAGE_OVERLAY_PERMISSION,
                Uri.parse("package:" + getPackageName()));
            startActivity(request);
            return;
        }
        startFloatingFavorites(false);
    }

    private void startFloatingFavorites(boolean visible) {
        Intent service = new Intent(this, FloatingFavoritesService.class)
            .setAction(visible ? FloatingFavoritesService.ACTION_SHOW : FloatingFavoritesService.ACTION_START);
        if (Build.VERSION.SDK_INT >= 26) startForegroundService(service);
        else startService(service);
    }

    private void showWeb(String baseUrl, String token) {
        activeWebBaseUrl = baseUrl;
        activeWebToken = token;
        webLoadFailureHandled = false;

        webView = new WebView(this);
        webView.setBackgroundColor(BG);
        webView.setLayerType(View.LAYER_TYPE_HARDWARE, null);

        WebSettings settings = webView.getSettings();
        settings.setJavaScriptEnabled(true);
        settings.setUserAgentString(settings.getUserAgentString() + " KSHRemote/IP15e");
        settings.setDomStorageEnabled(true);
        settings.setLoadWithOverviewMode(true);
        settings.setUseWideViewPort(true);
        settings.setMediaPlaybackRequiresUserGesture(false);
        settings.setCacheMode(WebSettings.LOAD_DEFAULT);
        settings.setOffscreenPreRaster(true);

        webView.setWebViewClient(new WebViewClient() {
            @Override public void onPageFinished(WebView view, String url) {
                super.onPageFinished(view, url);
                if (url != null && url.startsWith(activeWebBaseUrl + "/?")) {
                    view.evaluateJavascript("if(window.kshSetAutoFloating)kshSetAutoFloating("+
                        (prefs.getBoolean(PREF_EDGE_AUTO, false) ? "true" : "false")+");", null);
                }
            }
            @Override
            public boolean shouldOverrideUrlLoading(WebView view, String url) {
                return handleSpecialUrl(url);
            }

            @Override
            public boolean shouldOverrideUrlLoading(WebView view, WebResourceRequest request) {
                if (request == null || request.getUrl() == null) {
                    return false;
                }
                return handleSpecialUrl(request.getUrl().toString());
            }

            @Override
            public void onReceivedError(
                WebView view,
                int errorCode,
                String description,
                String failingUrl
            ) {
                if (
                    failingUrl == null
                    || activeWebBaseUrl.length() == 0
                    || failingUrl.startsWith(activeWebBaseUrl)
                ) {
                    handleWebLoadFailure(description);
                }
            }

            @Override
            public void onReceivedError(
                WebView view,
                WebResourceRequest request,
                WebResourceError error
            ) {
                if (request != null && request.isForMainFrame()) {
                    String description = "The remote page did not load.";
                    if (error != null && error.getDescription() != null) {
                        description = error.getDescription().toString();
                    }
                    handleWebLoadFailure(description);
                }
            }
        });

        setContentView(webView);
        immersive();
        applyDisplayRefreshPreference();

        try {
            String url = baseUrl
                + "/?token=" + URLEncoder.encode(token, "UTF-8")
                + "&v=" + System.currentTimeMillis();
            webView.loadUrl(url);
        } catch (Exception e) {
            showServerNotFoundUi(baseUrl, token, "URL error: " + e.getMessage());
        }
    }

    private void immersive() {
        View decor = getWindow().getDecorView();
        decor.setSystemUiVisibility(
            View.SYSTEM_UI_FLAG_IMMERSIVE_STICKY
            | View.SYSTEM_UI_FLAG_FULLSCREEN
            | View.SYSTEM_UI_FLAG_HIDE_NAVIGATION
            | View.SYSTEM_UI_FLAG_LAYOUT_FULLSCREEN
            | View.SYSTEM_UI_FLAG_LAYOUT_HIDE_NAVIGATION
            | View.SYSTEM_UI_FLAG_LAYOUT_STABLE
        );
    }

    @Override
    protected void onResume() {
        super.onResume();
        displayForeground = true;
        if (awaitingOverlayPermission) {
            awaitingOverlayPermission = false;
            if (webView != null) webView.evaluateJavascript("if(window.kshSetAutoFloating)kshSetAutoFloating("+
                (prefs.getBoolean(PREF_EDGE_AUTO, false) ? "true" : "false")+");", null);
        }
        if (FloatingFavoritesService.running) {
            startService(new Intent(this, FloatingFavoritesService.class)
                .setAction(FloatingFavoritesService.ACTION_HIDE));
        }
        immersive();
        displayManager = (DisplayManager) getSystemService(Context.DISPLAY_SERVICE);
        if (displayManager != null) displayManager.registerDisplayListener(displayListener, null);
        applyDisplayRefreshPreference();
    }

    @Override
    protected void onPause() {
        displayForeground = false;
        // Keep the drawer off the fullscreen Remote; show it only on backgrounding.
        if (!awaitingOverlayPermission && webView != null &&
            (prefs.getBoolean(PREF_EDGE_AUTO, false) || FloatingFavoritesService.running) &&
            (Build.VERSION.SDK_INT < 23 || Settings.canDrawOverlays(this))) {
            startFloatingFavorites(true);
        }
        requestViewRefresh(webView, 0f);
        requestViewRefresh(getWindow().getDecorView(), 0f);
        if (displayManager != null) displayManager.unregisterDisplayListener(displayListener);
        WindowManager.LayoutParams params = getWindow().getAttributes();
        params.preferredRefreshRate = 0;
        getWindow().setAttributes(params);
        super.onPause();
    }

    @Override
    public void onWindowFocusChanged(boolean hasFocus) {
        super.onWindowFocusChanged(hasFocus);
        if (hasFocus) {
            immersive();
            applyDisplayRefreshPreference();
        }
    }

    @Override
    public boolean onCreateOptionsMenu(android.view.Menu menu) {
        menu.add("App updates & display").setOnMenuItemClickListener(item -> {
            remoteUpdates.show(displayInfo()); return true;
        });
        return true;
    }

    @Override
    public void onBackPressed() {
        if (webView != null && webView.canGoBack()) {
            webView.goBack();
        } else {
            super.onBackPressed();
        }
    }
}
