package com.voterroll.app;

import android.app.Activity;
import android.app.AlertDialog;
import android.content.SharedPreferences;
import android.os.Bundle;
import android.view.Menu;
import android.view.MenuItem;
import android.webkit.JavascriptInterface;
import android.webkit.WebChromeClient;
import android.webkit.WebSettings;
import android.webkit.WebView;
import android.webkit.WebViewClient;
import android.widget.EditText;

public class MainActivity extends Activity {
    private static final String DEFAULT_URL = "http://192.168.1.156:8000/m";
    private WebView web;

    @Override
    protected void onCreate(Bundle savedInstanceState) {
        super.onCreate(savedInstanceState);
        setContentView(R.layout.activity_main);
        web = findViewById(R.id.web);
        WebSettings settings = web.getSettings();
        settings.setJavaScriptEnabled(true);
        settings.setDomStorageEnabled(true);
        settings.setAllowFileAccess(true);
        settings.setAllowUniversalAccessFromFileURLs(true);
        settings.setUseWideViewPort(true);
        settings.setLoadWithOverviewMode(true);
        web.addJavascriptInterface(new Host(), "AndroidHost");
        web.setWebViewClient(new WebViewClient());
        web.setWebChromeClient(new WebChromeClient());
        web.loadUrl("file:///android_asset/www/mobile.html");
    }

    private final class Host {
        @JavascriptInterface
        public String server() {
            return serverUrl().replaceAll("/m/?$", "").replaceAll("/+$", "");
        }
    }

    private SharedPreferences prefs() {
        return getSharedPreferences("voterroll", MODE_PRIVATE);
    }

    private String serverUrl() {
        return prefs().getString("url", DEFAULT_URL);
    }

    @Override
    public boolean onCreateOptionsMenu(Menu menu) {
        menu.add(0, 1, 0, "Server");
        return true;
    }

    @Override
    public boolean onOptionsItemSelected(MenuItem item) {
        if (item.getItemId() == 1) {
            EditText input = new EditText(this);
            input.setText(serverUrl());
            input.setSingleLine(true);
            new AlertDialog.Builder(this)
                    .setTitle("Server address")
                    .setMessage("Phone and this PC must be on the same Wi-Fi.")
                    .setView(input)
                    .setPositiveButton("Open", (d, w) -> {
                        String url = input.getText().toString().trim();
                        if (!url.startsWith("http")) url = "http://" + url;
                        url = url.replaceAll("/m/?$", "").replaceAll("/+$", "");
                        prefs().edit().putString("url", url).apply();
                        web.loadUrl("file:///android_asset/www/mobile.html");
                    })
                    .setNegativeButton("Cancel", null)
                    .show();
            return true;
        }
        return super.onOptionsItemSelected(item);
    }

    @Override
    public void onBackPressed() {
        if (web != null && web.canGoBack()) web.goBack();
        else super.onBackPressed();
    }
}
