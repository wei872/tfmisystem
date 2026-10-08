#include "GxVideoImageSave.h"
#include <QtWidgets/QApplication>
#include <QAbstractNativeEventFilter>
#include "GalaxyIncludes.h"

int main(int argc, char *argv[])
{
    QApplication a(argc, argv);

    GxVideoImageSave w;
    w.show();
    return a.exec();
}
