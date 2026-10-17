# ProfitOS iOS — build de validation

Le workflow GitHub Actions `Build ProfitOS iOS` génère le projet Capacitor sur un runner macOS, synchronise les plugins et compile une application **iOS Simulator non signée**. Cela valide la compilation native sans exiger de Mac local.

L'artefact `ProfitOS-iOS-Simulator` n'est **ni un IPA distribuable, ni une publication App Store**. Pour TestFlight/App Store, il faudra un compte Apple Developer, un App ID/bundle ID `fr.profitos.app`, la signature Apple et les profils/certificats correspondants. Les notifications réelles nécessitent aussi la configuration APNs.

## Validation minimale avant publication

Tester sur appareils réels : connexion/déconnexion, navigation, factures/PDF, justificatifs photo, Powens et autres redirections externes, reprise de session, permissions de notifications et ouverture d'une notification.
