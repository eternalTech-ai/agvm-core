# Third-Party Notices

Status: generated candidate notice inventory. Review with counsel before public release.

This file lists third-party runtime and UI dependencies included in the AGVM Core public export.
It is generated from `agvm_api/requirements.txt` and `agvm_cockpit_prototype/package-lock.json`.
It is not a substitute for legal review or for the final AGVM Core license decision.

## Summary

- Total dependencies: `196`
- Python dependencies: `10`
- NPM dependencies: `186`
- Dependencies requiring manual legal review: `4`
- Main AGVM Core license placeholder still present: `false`

## License Counts

| License | Count |
| --- | ---: |
| MIT | 165 |
| Apache-2.0 | 10 |
| ISC | 10 |
| BSD-3-Clause | 7 |
| BSD-3-Clause OR Apache-2.0 + PDFium dependency licenses | 1 |
| CC-BY-4.0 | 1 |
| MIT-CMU | 1 |
| pending_scan | 1 |

## Manual Review Queue

| Ecosystem | Package | Version / Specifier | License | Review |
| --- | --- | --- | --- | --- |
| python | `Pillow` | `Pillow>=10.0.0,<13.0.0` | MIT-CMU | required |
| python | `pypdfium2` | `pypdfium2>=4.30.0,<5.0.0` | BSD-3-Clause OR Apache-2.0 + PDFium dependency licenses | required |
| npm | `caniuse-lite` | `1.0.30001793` | CC-BY-4.0 | required |
| npm | `webgl-constants` | `1.1.1` | pending_scan | required |

## Python Dependencies

| Ecosystem | Package | Version / Specifier | License | Review |
| --- | --- | --- | --- | --- |
| python | `fastapi` | `fastapi==0.117.1` | MIT | standard |
| python | `Pillow` | `Pillow>=10.0.0,<13.0.0` | MIT-CMU | required |
| python | `playwright` | `playwright>=1.45.0,<2.0.0` | Apache-2.0 | standard |
| python | `pydantic` | `pydantic==2.11.9` | MIT | standard |
| python | `pypdf` | `pypdf>=5.0.0,<7.0.0` | BSD-3-Clause | standard |
| python | `pypdfium2` | `pypdfium2>=4.30.0,<5.0.0` | BSD-3-Clause OR Apache-2.0 + PDFium dependency licenses | required |
| python | `pytesseract` | `pytesseract>=0.3.13,<0.4.0` | Apache-2.0 | standard |
| python | `python-dotenv` | `python-dotenv==1.2.2` | BSD-3-Clause | standard |
| python | `python-multipart` | `python-multipart==0.0.20` | Apache-2.0 | standard |
| python | `uvicorn` | `uvicorn[standard]==0.37.0` | BSD-3-Clause | standard |

## NPM Dependencies

| Ecosystem | Package | Version / Specifier | License | Review |
| --- | --- | --- | --- | --- |
| npm | `@babel/code-frame` | `7.29.0` | MIT | standard |
| npm | `@babel/compat-data` | `7.29.3` | MIT | standard |
| npm | `@babel/core` | `7.29.0` | MIT | standard |
| npm | `@babel/generator` | `7.29.1` | MIT | standard |
| npm | `@babel/helper-compilation-targets` | `7.28.6` | MIT | standard |
| npm | `@babel/helper-globals` | `7.28.0` | MIT | standard |
| npm | `@babel/helper-module-imports` | `7.28.6` | MIT | standard |
| npm | `@babel/helper-module-transforms` | `7.28.6` | MIT | standard |
| npm | `@babel/helper-plugin-utils` | `7.28.6` | MIT | standard |
| npm | `@babel/helper-string-parser` | `7.27.1` | MIT | standard |
| npm | `@babel/helper-validator-identifier` | `7.28.5` | MIT | standard |
| npm | `@babel/helper-validator-option` | `7.27.1` | MIT | standard |
| npm | `@babel/helpers` | `7.29.2` | MIT | standard |
| npm | `@babel/parser` | `7.29.3` | MIT | standard |
| npm | `@babel/plugin-transform-react-jsx-self` | `7.27.1` | MIT | standard |
| npm | `@babel/plugin-transform-react-jsx-source` | `7.27.1` | MIT | standard |
| npm | `@babel/runtime` | `7.29.2` | MIT | standard |
| npm | `@babel/template` | `7.28.6` | MIT | standard |
| npm | `@babel/traverse` | `7.29.0` | MIT | standard |
| npm | `@babel/types` | `7.29.0` | MIT | standard |
| npm | `@dimforge/rapier3d-compat` | `0.12.0` | Apache-2.0 | standard |
| npm | `@esbuild/aix-ppc64` | `0.21.5` | MIT | standard |
| npm | `@esbuild/android-arm` | `0.21.5` | MIT | standard |
| npm | `@esbuild/android-arm64` | `0.21.5` | MIT | standard |
| npm | `@esbuild/android-x64` | `0.21.5` | MIT | standard |
| npm | `@esbuild/darwin-arm64` | `0.21.5` | MIT | standard |
| npm | `@esbuild/darwin-x64` | `0.21.5` | MIT | standard |
| npm | `@esbuild/freebsd-arm64` | `0.21.5` | MIT | standard |
| npm | `@esbuild/freebsd-x64` | `0.21.5` | MIT | standard |
| npm | `@esbuild/linux-arm` | `0.21.5` | MIT | standard |
| npm | `@esbuild/linux-arm64` | `0.21.5` | MIT | standard |
| npm | `@esbuild/linux-ia32` | `0.21.5` | MIT | standard |
| npm | `@esbuild/linux-loong64` | `0.21.5` | MIT | standard |
| npm | `@esbuild/linux-mips64el` | `0.21.5` | MIT | standard |
| npm | `@esbuild/linux-ppc64` | `0.21.5` | MIT | standard |
| npm | `@esbuild/linux-riscv64` | `0.21.5` | MIT | standard |
| npm | `@esbuild/linux-s390x` | `0.21.5` | MIT | standard |
| npm | `@esbuild/linux-x64` | `0.21.5` | MIT | standard |
| npm | `@esbuild/netbsd-x64` | `0.21.5` | MIT | standard |
| npm | `@esbuild/openbsd-x64` | `0.21.5` | MIT | standard |
| npm | `@esbuild/sunos-x64` | `0.21.5` | MIT | standard |
| npm | `@esbuild/win32-arm64` | `0.21.5` | MIT | standard |
| npm | `@esbuild/win32-ia32` | `0.21.5` | MIT | standard |
| npm | `@esbuild/win32-x64` | `0.21.5` | MIT | standard |
| npm | `@jridgewell/gen-mapping` | `0.3.13` | MIT | standard |
| npm | `@jridgewell/remapping` | `2.3.5` | MIT | standard |
| npm | `@jridgewell/resolve-uri` | `3.1.2` | MIT | standard |
| npm | `@jridgewell/sourcemap-codec` | `1.5.5` | MIT | standard |
| npm | `@jridgewell/trace-mapping` | `0.3.31` | MIT | standard |
| npm | `@mediapipe/tasks-vision` | `0.10.17` | Apache-2.0 | standard |
| npm | `@monogrid/gainmap-js` | `3.4.0` | MIT | standard |
| npm | `@pixi/colord` | `2.9.6` | MIT | standard |
| npm | `@react-three/drei` | `10.7.7` | MIT | standard |
| npm | `@react-three/fiber` | `9.6.1` | MIT | standard |
| npm | `@rolldown/pluginutils` | `1.0.0-beta.27` | MIT | standard |
| npm | `@rollup/rollup-android-arm-eabi` | `4.60.4` | MIT | standard |
| npm | `@rollup/rollup-android-arm64` | `4.60.4` | MIT | standard |
| npm | `@rollup/rollup-darwin-arm64` | `4.60.4` | MIT | standard |
| npm | `@rollup/rollup-darwin-x64` | `4.60.4` | MIT | standard |
| npm | `@rollup/rollup-freebsd-arm64` | `4.60.4` | MIT | standard |
| npm | `@rollup/rollup-freebsd-x64` | `4.60.4` | MIT | standard |
| npm | `@rollup/rollup-linux-arm-gnueabihf` | `4.60.4` | MIT | standard |
| npm | `@rollup/rollup-linux-arm-musleabihf` | `4.60.4` | MIT | standard |
| npm | `@rollup/rollup-linux-arm64-gnu` | `4.60.4` | MIT | standard |
| npm | `@rollup/rollup-linux-arm64-musl` | `4.60.4` | MIT | standard |
| npm | `@rollup/rollup-linux-loong64-gnu` | `4.60.4` | MIT | standard |
| npm | `@rollup/rollup-linux-loong64-musl` | `4.60.4` | MIT | standard |
| npm | `@rollup/rollup-linux-ppc64-gnu` | `4.60.4` | MIT | standard |
| npm | `@rollup/rollup-linux-ppc64-musl` | `4.60.4` | MIT | standard |
| npm | `@rollup/rollup-linux-riscv64-gnu` | `4.60.4` | MIT | standard |
| npm | `@rollup/rollup-linux-riscv64-musl` | `4.60.4` | MIT | standard |
| npm | `@rollup/rollup-linux-s390x-gnu` | `4.60.4` | MIT | standard |
| npm | `@rollup/rollup-linux-x64-gnu` | `4.60.4` | MIT | standard |
| npm | `@rollup/rollup-linux-x64-musl` | `4.60.4` | MIT | standard |
| npm | `@rollup/rollup-openbsd-x64` | `4.60.4` | MIT | standard |
| npm | `@rollup/rollup-openharmony-arm64` | `4.60.4` | MIT | standard |
| npm | `@rollup/rollup-win32-arm64-msvc` | `4.60.4` | MIT | standard |
| npm | `@rollup/rollup-win32-ia32-msvc` | `4.60.4` | MIT | standard |
| npm | `@rollup/rollup-win32-x64-gnu` | `4.60.4` | MIT | standard |
| npm | `@rollup/rollup-win32-x64-msvc` | `4.60.4` | MIT | standard |
| npm | `@tweenjs/tween.js` | `23.1.3` | MIT | standard |
| npm | `@types/babel__core` | `7.20.5` | MIT | standard |
| npm | `@types/babel__generator` | `7.27.0` | MIT | standard |
| npm | `@types/babel__template` | `7.4.4` | MIT | standard |
| npm | `@types/babel__traverse` | `7.28.0` | MIT | standard |
| npm | `@types/draco3d` | `1.4.10` | MIT | standard |
| npm | `@types/earcut` | `3.0.0` | MIT | standard |
| npm | `@types/estree` | `1.0.8` | MIT | standard |
| npm | `@types/offscreencanvas` | `2019.7.3` | MIT | standard |
| npm | `@types/react` | `19.2.15` | MIT | standard |
| npm | `@types/react-dom` | `19.2.3` | MIT | standard |
| npm | `@types/react-reconciler` | `0.28.9` | MIT | standard |
| npm | `@types/stats.js` | `0.17.4` | MIT | standard |
| npm | `@types/three` | `0.184.1` | MIT | standard |
| npm | `@types/webxr` | `0.5.24` | MIT | standard |
| npm | `@use-gesture/core` | `10.3.1` | MIT | standard |
| npm | `@use-gesture/react` | `10.3.1` | MIT | standard |
| npm | `@vitejs/plugin-react` | `4.7.0` | MIT | standard |
| npm | `@webgpu/types` | `0.1.70` | BSD-3-Clause | standard |
| npm | `@xmldom/xmldom` | `0.8.13` | MIT | standard |
| npm | `base64-js` | `1.5.1` | MIT | standard |
| npm | `baseline-browser-mapping` | `2.10.32` | Apache-2.0 | standard |
| npm | `bidi-js` | `1.0.3` | MIT | standard |
| npm | `browserslist` | `4.28.2` | MIT | standard |
| npm | `buffer` | `6.0.3` | MIT | standard |
| npm | `camera-controls` | `3.1.0` | MIT | standard |
| npm | `caniuse-lite` | `1.0.30001793` | CC-BY-4.0 | required |
| npm | `convert-source-map` | `2.0.0` | MIT | standard |
| npm | `cross-env` | `7.0.3` | MIT | standard |
| npm | `cross-spawn` | `7.0.6` | MIT | standard |
| npm | `csstype` | `3.2.3` | MIT | standard |
| npm | `debug` | `4.4.3` | MIT | standard |
| npm | `detect-gpu` | `5.0.70` | MIT | standard |
| npm | `draco3d` | `1.5.7` | Apache-2.0 | standard |
| npm | `earcut` | `3.0.2` | ISC | standard |
| npm | `electron-to-chromium` | `1.5.361` | ISC | standard |
| npm | `esbuild` | `0.21.5` | MIT | standard |
| npm | `escalade` | `3.2.0` | MIT | standard |
| npm | `eventemitter3` | `5.0.4` | MIT | standard |
| npm | `fflate` | `0.8.3` | MIT | standard |
| npm | `fflate` | `0.6.10` | MIT | standard |
| npm | `fsevents` | `2.3.3` | MIT | standard |
| npm | `gensync` | `1.0.0-beta.2` | MIT | standard |
| npm | `gifuct-js` | `2.1.2` | MIT | standard |
| npm | `glsl-noise` | `0.0.0` | MIT | standard |
| npm | `hls.js` | `1.6.16` | Apache-2.0 | standard |
| npm | `ieee754` | `1.2.1` | BSD-3-Clause | standard |
| npm | `immediate` | `3.0.6` | MIT | standard |
| npm | `is-promise` | `2.2.2` | MIT | standard |
| npm | `isexe` | `2.0.0` | ISC | standard |
| npm | `ismobilejs` | `1.1.1` | MIT | standard |
| npm | `its-fine` | `2.0.0` | MIT | standard |
| npm | `js-binary-schema-parser` | `2.0.3` | MIT | standard |
| npm | `js-tokens` | `4.0.0` | MIT | standard |
| npm | `jsesc` | `3.1.0` | MIT | standard |
| npm | `json5` | `2.2.3` | MIT | standard |
| npm | `lie` | `3.3.0` | MIT | standard |
| npm | `lru-cache` | `5.1.1` | ISC | standard |
| npm | `lucide-react` | `0.468.0` | ISC | standard |
| npm | `maath` | `0.10.8` | MIT | standard |
| npm | `meshline` | `3.3.1` | MIT | standard |
| npm | `meshoptimizer` | `1.1.1` | MIT | standard |
| npm | `ms` | `2.1.3` | MIT | standard |
| npm | `nanoid` | `3.3.12` | MIT | standard |
| npm | `node-releases` | `2.0.46` | MIT | standard |
| npm | `parse-svg-path` | `0.1.2` | MIT | standard |
| npm | `path-key` | `3.1.1` | MIT | standard |
| npm | `picocolors` | `1.1.1` | ISC | standard |
| npm | `pixi.js` | `8.18.1` | MIT | standard |
| npm | `postcss` | `8.5.15` | MIT | standard |
| npm | `potpack` | `1.0.2` | ISC | standard |
| npm | `promise-worker-transferable` | `1.0.4` | Apache-2.0 | standard |
| npm | `react` | `19.2.6` | MIT | standard |
| npm | `react-dom` | `19.2.6` | MIT | standard |
| npm | `react-refresh` | `0.17.0` | MIT | standard |
| npm | `react-use-measure` | `2.1.7` | MIT | standard |
| npm | `require-from-string` | `2.0.2` | MIT | standard |
| npm | `rollup` | `4.60.4` | MIT | standard |
| npm | `scheduler` | `0.27.0` | MIT | standard |
| npm | `semver` | `6.3.1` | ISC | standard |
| npm | `shebang-command` | `2.0.0` | MIT | standard |
| npm | `shebang-regex` | `3.0.0` | MIT | standard |
| npm | `source-map-js` | `1.2.1` | BSD-3-Clause | standard |
| npm | `stats-gl` | `2.4.2` | MIT | standard |
| npm | `stats.js` | `0.17.0` | MIT | standard |
| npm | `suspend-react` | `0.1.3` | MIT | standard |
| npm | `three` | `0.170.0` | MIT | standard |
| npm | `three` | `0.184.0` | MIT | standard |
| npm | `three-mesh-bvh` | `0.8.3` | MIT | standard |
| npm | `three-stdlib` | `2.36.1` | MIT | standard |
| npm | `tiny-lru` | `11.4.7` | BSD-3-Clause | standard |
| npm | `troika-three-text` | `0.52.4` | MIT | standard |
| npm | `troika-three-utils` | `0.52.4` | MIT | standard |
| npm | `troika-worker-utils` | `0.52.0` | MIT | standard |
| npm | `tunnel-rat` | `0.1.2` | MIT | standard |
| npm | `typescript` | `5.9.3` | Apache-2.0 | standard |
| npm | `update-browserslist-db` | `1.2.3` | MIT | standard |
| npm | `use-sync-external-store` | `1.6.0` | MIT | standard |
| npm | `utility-types` | `3.11.0` | MIT | standard |
| npm | `vite` | `5.4.21` | MIT | standard |
| npm | `webgl-constants` | `1.1.1` | pending_scan | required |
| npm | `webgl-sdf-generator` | `1.1.1` | MIT | standard |
| npm | `which` | `2.0.2` | ISC | standard |
| npm | `yallist` | `3.1.1` | ISC | standard |
| npm | `zustand` | `4.5.7` | MIT | standard |
| npm | `zustand` | `5.0.13` | MIT | standard |

## Release Note

Do not treat this notice inventory as approved until the final public license and third-party notice pack have been reviewed and approved.
