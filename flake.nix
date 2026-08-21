{
  description = "mb-cli-printer: label printing CLI, plus a shell for reversing printer protocols";

  inputs.nixpkgs.url = "github:NixOS/nixpkgs/nixos-unstable";

  outputs = { self, nixpkgs }:
    let
      systems = [ "x86_64-linux" "aarch64-linux" "x86_64-darwin" "aarch64-darwin" ];
      forAll = f: nixpkgs.lib.genAttrs systems (system: f nixpkgs.legacyPackages.${system});

      # The Brother iPrint&Label app, which speaks the QL-1110NWB raster protocol.
      defaultPackage = "com.brother.ptouch.iprintandlabel";
    in
    {
      packages = forAll (pkgs: rec {
        # Pull an installed app off a connected device, split APKs included.
        pull-apk = pkgs.writeShellApplication {
          name = "pull-apk";
          runtimeInputs = with pkgs; [ android-tools coreutils gnugrep ];
          text = ''
            pkg="''${1:-${defaultPackage}}"
            out="''${2:-apk/$pkg}"

            if [ -z "$(adb devices | grep -w device || true)" ]; then
              echo "no device in 'adb devices'." >&2
              echo "  plug in the phone, enable USB debugging, accept the pairing prompt," >&2
              echo "  then re-run. 'adb devices' should list it as 'device', not 'unauthorized'." >&2
              exit 1
            fi

            paths=$(adb shell pm path "$pkg" | tr -d '\r' | sed 's/^package://')
            if [ -z "$paths" ]; then
              echo "$pkg is not installed on this device." >&2
              echo "  list what is: adb shell pm list packages | grep -i brother" >&2
              exit 1
            fi

            mkdir -p "$out"
            count=0
            for p in $paths; do
              name=$(basename "$p")
              echo "pulling $p"
              adb pull "$p" "$out/$name" >/dev/null
              count=$((count + 1))
            done

            echo
            echo "pulled $count file(s) into $out:"
            ls -lh "$out"
            if [ "$count" -gt 1 ]; then
              echo
              echo "this is a split APK: base.apk holds the code, split_config.* the"
              echo "per-ABI native libraries and resources. Decompile base.apk, and"
              echo "unzip the arm64 split for lib/*.so if the protocol lives in native code."
            fi
          '';
        };

        # Decompile a pulled APK two ways: jadx for readable Java, apktool for smali
        # and resources. They disagree on hard cases, which is why both are useful.
        decompile-apk = pkgs.writeShellApplication {
          name = "decompile-apk";
          runtimeInputs = with pkgs; [ jadx apktool coreutils ];
          text = ''
            apk="''${1:-apk/${defaultPackage}/base.apk}"
            out="''${2:-decompiled/$(basename "$apk" .apk)}"

            if [ ! -f "$apk" ]; then
              echo "no such APK: $apk (run 'nix run .#pull-apk' first)" >&2
              exit 1
            fi

            mkdir -p "$out"
            echo "jadx -> $out/java (Java sources, best for reading logic)"
            jadx --no-res --show-bad-code -d "$out/java" "$apk" || \
              echo "jadx reported errors; partial output is still usable" >&2

            echo "apktool -> $out/smali (smali and resources, best for exact bytes)"
            apktool d -f -o "$out/smali" "$apk" >/dev/null

            echo
            echo "decompiled into $out"
            echo "start looking here:"
            echo "  rg -n 'ESC|0x1b|raster|Raster' $out/java | head"
            echo "  rg -n 'getBytes|write\\(|OutputStream' $out/java | head"
            echo "  ls $out/smali/lib   # native libraries, if any"
          '';
        };

        default = pull-apk;
      });

      apps = forAll (pkgs: {
        pull-apk = {
          type = "app";
          program = "${self.packages.${pkgs.stdenv.hostPlatform.system}.pull-apk}/bin/pull-apk";
        };
        decompile-apk = {
          type = "app";
          program = "${self.packages.${pkgs.stdenv.hostPlatform.system}.decompile-apk}/bin/decompile-apk";
        };
        default = self.apps.${pkgs.stdenv.hostPlatform.system}.pull-apk;
      });

      devShells = forAll (pkgs: {
        # Everything needed to pull an app apart and to run mbprint itself.
        default = pkgs.mkShell {
          packages = with pkgs; [
            # device access
            android-tools # adb, fastboot
            # bytecode
            jadx # dex -> readable Java
            apktool # dex -> smali, plus resources
            dex2jar # dex -> jar, for other Java tooling
            apksigner # re-sign a patched APK
            # native code and general poking
            radare2
            ripgrep
            jq
            unzip
            # protocol capture and analysis
            (python3.withPackages (ps: with ps; [ androguard pyshark ]))
            # mbprint itself
            uv
          ] ++ pkgs.lib.optionals pkgs.stdenv.hostPlatform.isLinux [
            wireshark-cli # tshark, for btsnoop_hci.log from the phone
            usbutils
            bluez
          ];

          shellHook = ''
            echo "reversing shell: adb, jadx, apktool, dex2jar, radare2, tshark, androguard"
            echo
            echo "  nix run .#pull-apk            pull ${defaultPackage}"
            echo "  nix run .#pull-apk -- PKG     pull another package"
            echo "  nix run .#decompile-apk       jadx + apktool on the pulled base.apk"
            echo
            echo "on Linux, adb needs udev rules or a running adb server as your user;"
            echo "if the device shows 'unauthorized', accept the prompt on the phone."
          '';
        };

        # Ghidra is a large closure, so it lives in its own shell.
        native = pkgs.mkShell {
          packages = with pkgs; [ ghidra radare2 bytecode-viewer ];
          shellHook = ''
            echo "native shell: ghidra, radare2, bytecode-viewer"
            echo "use this for lib/*.so from the arm64 split APK"
          '';
        };
      });
    };
}
