# LibnanTV Android App

A thin WebView wrapper APK that loads the live Libnan TV web app from GitHub Pages. Because it streams the page from `https://battbox.github.io/Battbox/libnan-tv.html` rather than bundling the HTML, the nightly stream sync continues to flow through automatically — install once, never reinstall.

## What's in here

```
android-app/
├── build.gradle.kts              # top-level Gradle config
├── settings.gradle.kts           # module declarations
├── gradle.properties             # JVM / AndroidX flags
└── app/
    ├── build.gradle.kts          # app module config (SDK levels, deps)
    ├── proguard-rules.pro        # ProGuard placeholder
    └── src/main/
        ├── AndroidManifest.xml   # phone + Android TV launcher entry
        ├── java/tv/libnan/app/
        │   └── MainActivity.kt   # the entire app (~150 lines)
        └── res/
            ├── drawable/         # adaptive icon vectors
            ├── mipmap-anydpi/    # icon fallback (Android 5–7)
            ├── mipmap-anydpi-v26/# adaptive icon (Android 8+)
            ├── values/           # strings, colors, theme
            └── xml/              # network security config
```

## Building

You don't need Android Studio. Every push to this folder triggers GitHub Actions to build a debug APK and attach it as a workflow artifact. To force a build:

1. Go to **Actions → Build LibnanTV APK** in the repo
2. 2. Click **Run workflow** → leave defaults → **Run workflow**
   3. 3. Wait ~3-5 minutes for the build to finish (green checkmark)
      4. 4. Open the run, scroll to **Artifacts**, download **LibnanTV-debug**
         5. 5. Unzip → you'll have `app-debug.apk`
           
            6. ## Installing on Android phones / tablets
           
            7. 1. Email or transfer `app-debug.apk` to the device
               2. 2. Tap the file
                  3. 3. If prompted, enable "Install unknown apps" for the file manager / browser you opened it from
                     4. 4. Tap **Install** → done
                       
                        5. ## Installing on Fire Stick / Android TV
                       
                        6. The easy way is using the **Downloader** app (free on Amazon Appstore / Play Store):
                       
                        7. 1. Install Downloader on the Fire Stick
                           2. 2. Enable "Apps from Unknown Sources" in Fire Stick's developer settings (Settings → My Fire TV → Developer options)
                              3. 3. Upload your APK somewhere fetchable (e.g. attach to a GitHub release, drop in Google Drive with public link, or use the artifact's direct URL)
                                 4. 4. In Downloader, paste the URL → it downloads and offers to install
                                   
                                    5. The alternative is `adb install app-debug.apk` over network ADB — faster if you have the tooling, but more setup.
                                   
                                    6. ## What the app actually does
                                   
                                    7. - Loads `https://battbox.github.io/Battbox/libnan-tv.html` in a fullscreen WebView
                                       - - Allows mixed-content (some HLS streams are HTTP, not HTTPS)
                                         - - Disables autoplay-blocking so live streams start without user gesture
                                           - - Handles HTML5 video fullscreen requests (locks landscape, hides UI)
                                             - - Hardware volume keys control media volume
                                               - - Back button: exits fullscreen video → goes back in WebView history → exits app
                                                 - - Keeps screen on during playback
                                                   - - Shows in both phone launcher and Android TV launcher (`LEANBACK_LAUNCHER`)
                                                    
                                                     - ## Configuration
                                                    
                                                     - The URL the app loads is hardcoded in `MainActivity.kt`:
                                                    
                                                     - ```kotlin
                                                       const val APP_URL = "https://battbox.github.io/Battbox/libnan-tv.html"
                                                       ```

                                                       If you ever rename the live page, change this constant and bump `versionCode` / `versionName` in `app/build.gradle.kts`, then push to trigger a rebuild.

                                                       ## Debug vs release builds

                                                       The CI currently builds a debug-signed APK, which is fine for sideloading but won't be accepted by the Play Store. To switch to a release build (signed with your own keystore), you'd need to:

                                                       1. Generate a keystore: `keytool -genkey -v -keystore libnan.jks -keyalg RSA -keysize 2048 -validity 10000 -alias libnan`
                                                       2. 2. Add the keystore + passwords as GitHub Actions secrets
                                                          3. 3. Update `app/build.gradle.kts` with a `signingConfigs` block
                                                             4. 4. Change CI to run `assembleRelease` instead of `assembleDebug`
                                                               
                                                                5. We can do that whenever you're ready to put this on the Play Store. For now, debug APKs are perfectly serviceable for sideloading.
