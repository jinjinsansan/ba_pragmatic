# Phase 0 基準値 — dga直結フィード実測 (2026-09-12 19:17 JST・日本の自宅回線)

取得方法: `BACOPY_DGA_ONLY=1 BACOPY_DGA_DIRECT=1 python collector_pragmatic.py --duration 70`
(camoufox を偽モジュールで遮断した状態 = ブラウザ完全不使用で成立することを確認済み)

## 結果

| 項目 | 値 |
|---|---|
| 接続 | `wss://dga.pragmaticplaylive.net/ws` へ **1.1秒で確立** |
| casinoId | `ppcds00000003709` (従来値のまま**有効**) |
| 70秒間の受信 | msgs=625 / shuffles=3 / **tables=60** |
| 終了 | exit=0 (クリーン) |

★`STAKE SPEED BACCARAT` が含まれる = **この casinoId は今も Stake のもの**。
★Cloudflare 非経由(AWS)なので `stake.com` の 451 とは無関係に生きている。

## ★ミラー検証用の基準 — 卓ID → qpid 対応 (60卓)

Phase 0 でミラーにログインした際、**この qpid と一致するか**を突き合わせること。
一致すれば「どのドメインでも同じ設定で動く」= フェイルオーバー設計が成立する。

| table_id | qpid | 卓名 |
|---|---|---|
| 007 | `007` | BACCARAT_MULTIPLAY |
| 2101 | `a10megasicbaca10` | Mega Sic Bac |
| 401 | `h22z8qhp17sa0vkh` | Baccarat 1 |
| 402 | `pwnhicogrzeodk79` | Speed Baccarat 1 |
| 403 | `kkqnazmd8ttq7fgd` | Speed Baccarat 2 |
| 404 | `9j3eagurfwmml7z2` | Baccarat 2 |
| 405 | `b0jf7rlboleibnap` | Speed Baccarat 18 |
| 411 | `ne074fgn4bd1150i` | Baccarat 5 |
| 412 | `s8s9f0quk3ygiyb1` | Speed Baccarat 3 |
| 413 | `oq808ojps709qqaf` | Baccarat 6 |
| 414 | `886ewimul28yw14j` | Speed Baccarat 5 |
| 415 | `2q57e43m4ivqwaq3` | Speed Baccarat 6 |
| 421 | `cbcf6qas8fscb221` | Speed Baccarat 12 |
| 422 | `cbcf6qas8fscb222` | Baccarat 3 |
| 424 | `cbcf6qas8fscb224` | Speed Baccarat 11 |
| 425 | `bcpirpmfpeobc191` | Baccarat 7 |
| 426 | `bcpirpmfpeobc192` | Turbo Baccarat |
| 427 | `bcpirpmfpeobc193` | Speed Baccarat 15 |
| 430 | `bcpirpmfpeobc196` | Speed Baccarat 9 |
| 432 | `bcpirpmfpeobc198` | Speed Baccarat 8 |
| 433 | `bcpirpmfpeobc199` | Super 8 Baccarat |
| 434 | `bcpirpmfpobc1910` | Fortune 6 Baccarat |
| 435 | `bcpirpmfpobc1911` | Speed Baccarat 16 |
| 436 | `bcpirpmfpobc1912` | Baccarat 9 |
| 438 | `m88hicogrzeod202` | Speed Baccarat 13 |
| 439 | `bcpirpmfpebc1908` | Speed Baccarat 17 |
| 440 | `bc341stakelbc341` | STAKE SPEED BACCARAT |
| 441 | `bc281koreanch281` | Korean Speed Baccarat 1 |
| 442 | `mbc371rpmfmbc371` | MEGA BACCARAT |
| 449 | `bc392chromabc392` | Korean Speed Baccarat 2 |
| 450 | `speedbca18generi` | Indonesian Speed Baccarat 1 |
| 451 | `speedbca14gesbc1` | Thai Speed Baccarat 1 |
| 4511 | `privatesqueeze01` | Privé Lounge Baccarat Squeeze 1 |
| 4512 | `privatesqueeze02` | Privé Lounge Baccarat Squeeze 2 |
| 452 | `speedbca14gesbc2` | Thai Speed Baccarat 2 |
| 453 | `baccaratspstua13` | Thai Baccarat 1 |
| 454 | `privbca51privbc1` | Privé Lounge Baccarat 1 |
| 455 | `privbca52privbc2` | Privé Lounge Baccarat 2 |
| 456 | `privbca53privbc3` | Privé Lounge Baccarat 3 |
| 458 | `privbca55privbc5` | Privé Lounge Baccarat 5 |
| 459 | `tobc51koreanto51` | Korean Speed Baccarat 3 |
| 460 | `tobc52koreanto52` | Korean Baccarat 1 |
| 461 | `bca51kprivlobca1` | Korean Privé Lounge Baccarat 1 |
| 466 | `bca51priv16bca51` | Privé Lounge Baccarat 6 |
| 467 | `bca51priv17bca51` | Privé Lounge Baccarat 7 |
| 468 | `bca51priv18bca51` | Privé Lounge Baccarat 8 |
| 476 | `tobcspbaccarat61` | Korean Speed Baccarat 5 |
| 477 | `tobcspbaccarat62` | Korean Turbo Baccarat 1 |
| 479 | `spto11bctorobc11` | Vietnamese Speed Baccarat 1 |
| 480 | `spto12bctorobc12` | Vietnamese Speed Baccarat 2 |
| 481 | `spijbnsu408vmv71` | Chinese Speed Baccarat 1 |
| 482 | `spijbnsu408vmv72` | Chinese Speed Baccarat 2 |
| 483 | `spijbnsu408vmv73` | Chinese Speed Baccarat 3 |
| 484 | `spto21bctorobc21` | Vietnamese Speed Baccarat 3 |
| 488 | `spto41bctorobc41` | Japanese Speed Baccarat 1 |
| 489 | `spto42bctorobc42` | Japanese Speed Baccarat 2 |
| 490 | `spto43bctorobc43` | Japanese Speed Baccarat 3 |
| 496 | `spvtbc281vsbc281` | Indonesian Speed Baccarat 2 |
| 499 | `tobcspbaccarat92` | Korean Baccarat 2 |
| 851 | `bcadigitalsqz001` | Squeeze Baccarat |
