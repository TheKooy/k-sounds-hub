#include <QAction>
#include <QByteArray>
#include <QCommandLineOption>
#include <QCommandLineParser>
#include <QCoreApplication>
#include <QGuiApplication>
#include <QHash>
#include <QKeySequence>
#include <QList>
#include <QObject>
#include <QSocketNotifier>
#include <QString>
#include <QStringList>
#include <QTextStream>

#include <KGlobalAccel>

#include <unistd.h>

class KGlobalAccelHelper final : public QObject {
    Q_OBJECT

public:
    explicit KGlobalAccelHelper(QString appId, QObject *parent = nullptr)
        : QObject(parent), m_appId(std::move(appId))
    {
    }

    int startDaemon()
    {
        if (!setupBackend()) {
            return 2;
        }
        QTextStream(stdout) << "READY\tKGLOBALACCEL" << Qt::endl;
        setupStdinCommands();
        return -1;
    }

    int startLegacy(const QString &shortcutId, const QString &description, const QString &initialTrigger)
    {
        if (!setupBackend()) {
            return 2;
        }
        if (!registerAction(shortcutId, description, initialTrigger)) {
            return 3;
        }
        m_legacyShortcutId = shortcutId;
        setupStdinCommands();
        return -1;
    }

public slots:
    void onStdinActivated()
    {
        char buffer[4096];
        const ssize_t count = ::read(STDIN_FILENO, buffer, sizeof(buffer));
        if (count <= 0) {
            if (m_stdinNotifier) {
                m_stdinNotifier->setEnabled(false);
            }
            return;
        }

        m_stdinBuffer.append(buffer, static_cast<qsizetype>(count));
        while (true) {
            const qsizetype newline = m_stdinBuffer.indexOf('\n');
            if (newline < 0) {
                break;
            }
            const QByteArray line = m_stdinBuffer.left(newline);
            m_stdinBuffer.remove(0, newline + 1);
            handleCommand(QString::fromUtf8(line));
        }
    }

private:
    bool setupBackend()
    {
        auto *accel = KGlobalAccel::self();
        if (accel == nullptr) {
            fail(QString(), QStringLiteral("KGlobalAccel service unavailable"));
            return false;
        }
        if (m_backendConnected) {
            return true;
        }

        connect(
            accel,
            &KGlobalAccel::globalShortcutActiveChanged,
            this,
            [this](QAction *action, bool active) {
                const QString id = m_idsByAction.value(action);
                if (id.isEmpty()) {
                    return;
                }
                QTextStream(stdout)
                    << (active ? "ACTIVATED\t" : "DEACTIVATED\t")
                    << id << Qt::endl;
            }
        );
        connect(
            accel,
            &KGlobalAccel::globalShortcutChanged,
            this,
            [this](QAction *action, const QKeySequence &) {
                const QString id = m_idsByAction.value(action);
                if (!id.isEmpty()) {
                    reportSync(id);
                }
            }
        );
        m_backendConnected = true;
        return true;
    }

    static QKeySequence parsePortable(const QString &text)
    {
        const QString clean = text.trimmed();
        if (clean.isEmpty()) {
            return {};
        }
        const QKeySequence parsed = QKeySequence::fromString(clean, QKeySequence::PortableText);
        if (parsed.count() > 1) {
            return QKeySequence(parsed[0]);
        }
        return parsed;
    }

    static QString portable(const QKeySequence &sequence)
    {
        return sequence.toString(QKeySequence::PortableText).trimmed();
    }

    static QString sanitized(QString value)
    {
        value.replace('\t', ' ');
        value.replace('\n', ' ');
        value.replace('\r', ' ');
        return value.trimmed();
    }

    static bool reservedSequence(const QKeySequence &sequence)
    {
        if (sequence.isEmpty()) {
            return false;
        }
        const auto combination = sequence[0];
        const int key = combination.key();
        return key == Qt::Key_Return
            || key == Qt::Key_Enter
            || key == Qt::Key_Backspace
            || key == Qt::Key_Delete
            || key == Qt::Key_Escape
            || key == Qt::Key_Tab
            || key == Qt::Key_Backtab
            || key == Qt::Key_Shift
            || key == Qt::Key_Control
            || key == Qt::Key_Alt
            || key == Qt::Key_Meta
            || key == Qt::Key_AltGr
            || key == Qt::Key_CapsLock
            || key == Qt::Key_NumLock
            || key == Qt::Key_ScrollLock;
    }

    QAction *actionFor(const QString &shortcutId) const
    {
        return m_actions.value(shortcutId, nullptr);
    }

    QList<QKeySequence> currentShortcuts(const QString &shortcutId) const
    {
        QAction *action = actionFor(shortcutId);
        if (!action) {
            return {};
        }
        return KGlobalAccel::self()->shortcut(action);
    }

    QString describeConflicts(const QKeySequence &sequence) const
    {
        const auto conflicts = KGlobalAccel::globalShortcutsByKey(sequence);
        QStringList labels;
        for (const auto &info : conflicts) {
            QString component = sanitized(info.componentFriendlyName());
            if (component.isEmpty()) {
                component = sanitized(info.componentUniqueName());
            }
            QString action = sanitized(info.friendlyName());
            if (action.isEmpty()) {
                action = sanitized(info.uniqueName());
            }
            QString label = component;
            if (!action.isEmpty()) {
                if (!label.isEmpty()) {
                    label += QStringLiteral(" — ");
                }
                label += action;
            }
            if (label.isEmpty()) {
                label = QStringLiteral("another global shortcut");
            }
            labels.append(label);
        }
        labels.removeDuplicates();
        return labels.join(QStringLiteral(" | "));
    }

    bool conflictIsOnlyThisLogicalAction(const QString &shortcutId, const QKeySequence &sequence) const
    {
        const auto conflicts = KGlobalAccel::globalShortcutsByKey(sequence);
        if (conflicts.isEmpty()) {
            return false;
        }
        for (const auto &info : conflicts) {
            const QString actionId = info.uniqueName().trimmed();
            const QString componentId = info.componentUniqueName().trimmed();
            const QString componentFriendly = info.componentFriendlyName().trimmed();
            const bool sameAction = actionId == shortcutId;
            const bool sameComponent = componentId == m_appId
                || componentFriendly.contains(QStringLiteral("K-Sounds"), Qt::CaseInsensitive)
                || componentId.contains(QStringLiteral("k-sounds"), Qt::CaseInsensitive);
            if (!(sameAction && sameComponent)) {
                return false;
            }
        }
        return true;
    }

    bool registerAction(const QString &rawId, const QString &rawDescription, const QString &initialTrigger)
    {
        const QString shortcutId = sanitized(rawId);
        const QString description = sanitized(rawDescription);
        if (shortcutId.isEmpty() || description.isEmpty()) {
            fail(shortcutId, QStringLiteral("missing shortcut id or description"));
            return false;
        }

        const QKeySequence initial = parsePortable(initialTrigger);
        if (reservedSequence(initial)) {
            fail(shortcutId, QStringLiteral("reserved-sequence"));
            return false;
        }

        QAction *action = actionFor(shortcutId);
        if (!action) {
            action = new QAction(this);
            action->setObjectName(shortcutId);
            action->setText(description);
            action->setProperty("componentName", m_appId);
            action->setProperty("componentDisplayName", QStringLiteral("K-Sounds"));
            m_actions.insert(shortcutId, action);
            m_idsByAction.insert(action, shortcutId);

            const QList<QKeySequence> initialList = initial.isEmpty()
                ? QList<QKeySequence>{}
                : QList<QKeySequence>{initial};

            auto *accel = KGlobalAccel::self();
            accel->setDefaultShortcut(action, initialList, KGlobalAccel::Autoloading);
            if (!accel->setShortcut(action, initialList, KGlobalAccel::Autoloading)) {
                if (!accel->setShortcut(action, QList<QKeySequence>{}, KGlobalAccel::NoAutoloading)) {
                    fail(shortcutId, QStringLiteral("could not register shortcut with KGlobalAccel"));
                    return false;
                }
                QTextStream(stdout)
                    << "DEFAULT_CONFLICT\t" << shortcutId << "\t" << portable(initial) << Qt::endl;
            }
        } else {
            action->setText(description);
        }

        QTextStream(stdout) << "READY\t" << shortcutId << "\tKGLOBALACCEL" << Qt::endl;
        reportSync(shortcutId);
        return true;
    }

    void reportSync(const QString &shortcutId)
    {
        const QList<QKeySequence> shortcuts = currentShortcuts(shortcutId);
        QStringList values;
        for (const auto &sequence : shortcuts) {
            const QString value = portable(sequence);
            if (!value.isEmpty()) {
                values.append(value);
            }
        }

        QTextStream out(stdout);
        if (values.isEmpty()) {
            out << "SYNC\t" << shortcutId << "\tDISABLED\t" << Qt::endl;
        } else {
            out << "SYNC\t" << shortcutId << "\tENABLED\t"
                << values.join(QStringLiteral(", ")) << Qt::endl;
        }
    }

    void handleCommand(const QString &raw)
    {
        const QString line = raw.trimmed();
        if (line.isEmpty()) {
            return;
        }
        const QStringList parts = line.split('\t', Qt::KeepEmptyParts);
        const QString command = parts.value(0).trimmed().toUpper();

        if (command == QStringLiteral("REGISTER")) {
            registerAction(parts.value(1), parts.value(2), parts.mid(3).join(QStringLiteral("\t")));
            return;
        }
        if (command == QStringLiteral("REFRESH")) {
            QString id = parts.value(1).trimmed();
            if (id.isEmpty()) {
                id = m_legacyShortcutId;
            }
            if (!id.isEmpty()) {
                reportSync(id);
            }
            return;
        }
        if (command == QStringLiteral("SET")) {
            if (parts.size() >= 3) {
                setShortcut(parts.value(1), parts.mid(2).join(QStringLiteral("\t")));
            } else {
                setShortcut(m_legacyShortcutId, parts.value(1));
            }
            return;
        }
        if (command == QStringLiteral("PING")) {
            QTextStream(stdout) << "PONG\tKGLOBALACCEL" << Qt::endl;
            return;
        }

        fail(QString(), QStringLiteral("unknown-command: ") + command);
    }

    void setShortcut(const QString &shortcutId, const QString &trigger)
    {
        QAction *action = actionFor(shortcutId);
        if (!action) {
            fail(shortcutId, QStringLiteral("shortcut-not-ready"), QStringLiteral("SET_ERROR"));
            return;
        }

        const QKeySequence sequence = parsePortable(trigger);
        if (!trigger.trimmed().isEmpty() && sequence.isEmpty()) {
            fail(shortcutId, QStringLiteral("invalid-sequence: ") + trigger, QStringLiteral("SET_ERROR"));
            reportSync(shortcutId);
            return;
        }
        if (reservedSequence(sequence)) {
            fail(shortcutId, QStringLiteral("reserved-sequence"), QStringLiteral("SET_ERROR"));
            reportSync(shortcutId);
            return;
        }

        if (!sequence.isEmpty()) {
            const QList<QKeySequence> current = currentShortcuts(shortcutId);
            const bool alreadyCurrent = current.size() == 1 && current.constFirst() == sequence;
            if (!alreadyCurrent && !KGlobalAccel::isGlobalShortcutAvailable(sequence)) {
                if (conflictIsOnlyThisLogicalAction(shortcutId, sequence)) {
                    KGlobalAccel::stealShortcutSystemwide(sequence);
                } else {
                    QTextStream(stdout)
                        << "CONFLICT\t" << shortcutId << "\t" << portable(sequence) << "\t"
                        << describeConflicts(sequence) << Qt::endl;
                    reportSync(shortcutId);
                    return;
                }
            }
        }

        const QList<QKeySequence> shortcuts = sequence.isEmpty()
            ? QList<QKeySequence>{}
            : QList<QKeySequence>{sequence};

        const bool ok = KGlobalAccel::self()->setShortcut(
            action,
            shortcuts,
            KGlobalAccel::NoAutoloading
        );
        if (!ok) {
            fail(shortcutId, QStringLiteral("setShortcut-failed: ") + portable(sequence), QStringLiteral("SET_ERROR"));
            reportSync(shortcutId);
            return;
        }

        QTextStream(stdout)
            << "SET_OK\t" << shortcutId << "\t" << portable(sequence) << Qt::endl;
        reportSync(shortcutId);
    }

    void setupStdinCommands()
    {
        m_stdinNotifier = new QSocketNotifier(STDIN_FILENO, QSocketNotifier::Read, this);
        // Qt 6.11's activated() signal carries an internal QPrivateSignal.
        // A typed functor connection may consume fewer public arguments than
        // the signal provides, which keeps this compatible without naming the
        // private argument. The old string-based two-argument signature can
        // silently fail at runtime, leaving daemon stdin unread.
        const auto connection = QObject::connect(
            m_stdinNotifier,
            &QSocketNotifier::activated,
            this,
            [this](QSocketDescriptor, QSocketNotifier::Type) {
                onStdinActivated();
            }
        );
        if (!connection) {
            fail(QString(), QStringLiteral("stdin-notifier-connect-failed"));
            QCoreApplication::exit(70);
        }
    }

    void fail(const QString &shortcutId, const QString &message, const QString &event = QStringLiteral("ERROR"))
    {
        QTextStream out(stdout);
        out << event << "\t";
        if (!shortcutId.isEmpty()) {
            out << shortcutId << "\t";
        }
        out << sanitized(message) << Qt::endl;
    }

private:
    QString m_appId;
    QString m_legacyShortcutId;
    QHash<QString, QAction *> m_actions;
    QHash<QAction *, QString> m_idsByAction;
    QSocketNotifier *m_stdinNotifier = nullptr;
    QByteArray m_stdinBuffer;
    bool m_backendConnected = false;
};

int main(int argc, char **argv)
{
    QGuiApplication app(argc, argv);

    QCommandLineParser parser;
    parser.addHelpOption();

    QCommandLineOption daemonOption(
        QStringList{QStringLiteral("daemon")},
        QStringLiteral("Run one shared helper process accepting REGISTER/SET/REFRESH commands")
    );
    QCommandLineOption appIdOption(
        QStringList{QStringLiteral("app-id")},
        QStringLiteral("KGlobalAccel component/application id"),
        QStringLiteral("id"),
        QStringLiteral("k-sounds-hub")
    );
    QCommandLineOption shortcutIdOption(
        QStringList{QStringLiteral("shortcut-id")},
        QStringLiteral("Stable shortcut id (legacy single-action mode)"),
        QStringLiteral("id")
    );
    QCommandLineOption descriptionOption(
        QStringList{QStringLiteral("description")},
        QStringLiteral("Shortcut description (legacy single-action mode)"),
        QStringLiteral("text")
    );
    QCommandLineOption triggerOption(
        QStringList{QStringLiteral("trigger")},
        QStringLiteral("Portable Qt key sequence used as the first/default binding"),
        QStringLiteral("trigger")
    );

    parser.addOption(daemonOption);
    parser.addOption(appIdOption);
    parser.addOption(shortcutIdOption);
    parser.addOption(descriptionOption);
    parser.addOption(triggerOption);
    parser.process(app);

    const QString appId = parser.value(appIdOption).trimmed();
    const QString shortcutId = parser.value(shortcutIdOption).trimmed();
    const QString description = parser.value(descriptionOption).trimmed();
    const QString trigger = parser.value(triggerOption).trimmed();

    if (appId.isEmpty()) {
        QTextStream(stdout) << "ERROR\tmissing --app-id" << Qt::endl;
        return 64;
    }

    QCoreApplication::setApplicationName(appId);
    QCoreApplication::setOrganizationName(QStringLiteral("K-Sounds"));
    QGuiApplication::setApplicationDisplayName(QStringLiteral("K-Sounds"));

    KGlobalAccelHelper helper(appId);
    int result = -1;
    if (parser.isSet(daemonOption)) {
        result = helper.startDaemon();
    } else {
        if (shortcutId.isEmpty() || description.isEmpty()) {
            QTextStream(stdout)
                << "ERROR\tmissing --shortcut-id or --description"
                << Qt::endl;
            return 64;
        }
        result = helper.startLegacy(shortcutId, description, trigger);
    }

    if (result >= 0) {
        return result;
    }
    return app.exec();
}

#include "main.moc"
