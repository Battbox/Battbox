package tv.libnan.app

import android.annotation.SuppressLint
import android.content.pm.ActivityInfo
import android.graphics.Color
import android.graphics.drawable.ColorDrawable
import android.media.AudioManager
import android.os.Bundle
import android.view.View
import android.view.ViewGroup
import android.view.WindowManager
import android.webkit.PermissionRequest
import android.webkit.WebChromeClient
import android.webkit.WebSettings
import android.webkit.WebView
import android.webkit.WebViewClient
import android.widget.FrameLayout
import androidx.activity.ComponentActivity
import androidx.activity.OnBackPressedCallback
import androidx.core.view.WindowCompat
import androidx.core.view.WindowInsetsCompat
import androidx.core.view.WindowInsetsControllerCompat

class MainActivity : ComponentActivity() {

      private companion object {
                const val APP_URL = "https://battbox.github.io/Battbox/libnan-tv.html"
      }

          private lateinit var rootView: FrameLayout
      private lateinit var webView: WebView
      private var customView: View? = null
      private var customViewCallback: WebChromeClient.CustomViewCallback? = null

      @SuppressLint("SetJavaScriptEnabled")
          override fun onCreate(savedInstanceState: Bundle?) {
                    super.onCreate(savedInstanceState)

                            // Edge-to-edge, black background, screen stays on during playback
                                    WindowCompat.setDecorFitsSystemWindows(window, false)
                                            window.setBackgroundDrawable(ColorDrawable(Color.BLACK))
                                                    window.addFlags(WindowManager.LayoutParams.FLAG_KEEP_SCREEN_ON)

                                                            // Hardware volume keys control media volume (not ringer)
                                                                    volumeControlStream = AudioManager.STREAM_MUSIC

                    hideSystemBars()

                            // Build the view hierarchy programmatically
                                    rootView = FrameLayout(this).apply {
                                                  setBackgroundColor(Color.BLACK)
                                                              layoutParams = ViewGroup.LayoutParams(
                                                                                ViewGroup.LayoutParams.MATCH_PARENT,
                                                                                ViewGroup.LayoutParams.MATCH_PARENT
                                                                            )
                                    }
                                            setContentView(rootView)

                                                    webView = WebView(this).apply {
                                                                  setBackgroundColor(Color.BLACK)
                                                                              layoutParams = ViewGroup.LayoutParams(
                                                                                                ViewGroup.LayoutParams.MATCH_PARENT,
                                                                                                ViewGroup.LayoutParams.MATCH_PARENT
                                                                                            )
                                                    }
                                                            rootView.addView(webView)
                                                                    configureWebView(webView)
                                                                            webView.loadUrl(APP_URL)

                                                                                    // Modern back-press handling: WebView history -> exit fullscreen -> fall through
                                                                                            onBackPressedDispatcher.addCallback(this, object : OnBackPressedCallback(true) {
                                                                                                          override fun handleOnBackPressed() {
                                                                                                                            when {
                                                                                                                                                  customView != null -> {
                                                                                                                                                                            // Exit HTML5 fullscreen video first
                                                                                                                                                                            (webView.webChromeClient as? WebChromeClient)?.onHideCustomView()
                                                                                                                                                  }
                                                                                                                                                                      webView.canGoBack() -> webView.goBack()
                                                                                                                                                                                          else -> {
                                                                                                                                                                                                                    // No history left - exit app
                                                                                                                                                                                                                    isEnabled = false
                                                                                                                                                                                                                    onBackPressedDispatcher.onBackPressed()
                                                                                                                                                                                                                                        }
                                                                                                                                                                                                          }
                                                                                                          }
                                                                                            })
          }

              @SuppressLint("SetJavaScriptEnabled")
                  private fun configureWebView(wv: WebView) {
                            wv.settings.apply {
                                          javaScriptEnabled = true
                                          domStorageEnabled = true
                                          databaseEnabled = true

                                          // Don't expose local file system to a remote page
                                          allowFileAccess = false
                                          allowContentAccess = false

                                          // HLS streams autoplay without user gesture
                                          mediaPlaybackRequiresUserGesture = false

                                          // Some live channels are HTTP not HTTPS - allow mixed content
                                          mixedContentMode = WebSettings.MIXED_CONTENT_ALWAYS_ALLOW

                                          // Standard caching, viewport
                                          cacheMode = WebSettings.LOAD_DEFAULT
                                          useWideViewPort = true
                                          loadWithOverviewMode = true

                                          // No zoom UI - the web app handles its own layout
                                          setSupportZoom(false)
                                                      builtInZoomControls = false
                                          displayZoomControls = false
                            }

                                    WebView.setWebContentsDebuggingEnabled(true)

                                            wv.webViewClient = object : WebViewClient() {
                                                          override fun shouldOverrideUrlLoading(view: WebView, url: String): Boolean {
                                                                            // Stay in WebView for everything - let it navigate internally
                                                                            return false
                                                          }
                                            }

                                                    wv.webChromeClient = object : WebChromeClient() {
                                                                  // HTML5 video requested fullscreen -> swap WebView for the video view
                                                                  override fun onShowCustomView(view: View, callback: CustomViewCallback) {
                                                                                    if (customView != null) {
                                                                                                          // Already in fullscreen - abort the new request
                                                                                                          callback.onCustomViewHidden()
                                                                                                                              return
                                                                                    }
                                                                                                    customView = view
                                                                                    customViewCallback = callback
                                                                                    rootView.addView(
                                                                                                          view,
                                                                                                          FrameLayout.LayoutParams(
                                                                                                                                    ViewGroup.LayoutParams.MATCH_PARENT,
                                                                                                                                    ViewGroup.LayoutParams.MATCH_PARENT
                                                                                                                                )
                                                                                                                          )
                                                                                                    webView.visibility = View.GONE
                                                                                    requestedOrientation = ActivityInfo.SCREEN_ORIENTATION_SENSOR_LANDSCAPE
                                                                                    hideSystemBars()
                                                                  }

                                                                              override fun onHideCustomView() {
                                                                                                customView?.let { rootView.removeView(it) }
                                                                                                                customView = null
                                                                                                webView.visibility = View.VISIBLE
                                                                                                customViewCallback?.onCustomViewHidden()
                                                                                                                customViewCallback = null
                                                                                                requestedOrientation = ActivityInfo.SCREEN_ORIENTATION_SENSOR
                                                                                                hideSystemBars()
                                                                              }

                                                                                          // Auto-grant any media permissions the page asks for
                                                                                                      override fun onPermissionRequest(request: PermissionRequest) {
                                                                                                                        request.grant(request.resources)
                                                                                                      }
                                                    }
                  }

                      private fun hideSystemBars() {
                                val controller = WindowInsetsControllerCompat(window, window.decorView)
                                        controller.hide(WindowInsetsCompat.Type.systemBars())
                                                controller.systemBarsBehavior =
                                    WindowInsetsControllerCompat.BEHAVIOR_SHOW_TRANSIENT_BARS_BY_SWIPE
                      }

                          override fun onWindowFocusChanged(hasFocus: Boolean) {
                                    super.onWindowFocusChanged(hasFocus)
                                            if (hasFocus) hideSystemBars()
                          }

                              override fun onPause() {
                                        super.onPause()
                                                webView.onPause()
                              }

                                  override fun onResume() {
                                            super.onResume()
                                                    webView.onResume()
                                                            hideSystemBars()
                                  }

                                      override fun onDestroy() {
                                                // Tear down WebView cleanly to avoid leaks
                                                if (::webView.isInitialized) {
                                                              (webView.parent as? ViewGroup)?.removeView(webView)
                                                                          webView.destroy()
                                                }
                                                        super.onDestroy()
                                      }
}
