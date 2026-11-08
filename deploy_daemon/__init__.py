"""配備係（deploy daemon）。

郵便受け Worker の「変更の受け取り口」を見回り、届け出を受け取り、
GitHub から変更を取り、検証用コピーでテストし、実チェックアウトへ反映し、
常駐を再起動し、生存確認とロールバックまでを行う PC 側の独立プログラム。
tray-tools 常駐には組み込まない（再起動の外側にいる必要があるため）。

設計の正本は collab-workspace の docs/system-spec.html。
"""

__version__ = "0.1.0"
