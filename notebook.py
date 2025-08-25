import sys, os, tempfile
from PyQt5.QtWidgets import (
    QApplication, QMainWindow, QAction, QFileDialog, QSlider, QLabel, QToolBar,
    QMessageBox, QListWidget, QDockWidget, QPushButton, QWidget, QHBoxLayout,
    QVBoxLayout, QShortcut
)
from PyQt5.QtGui import QPainter, QPen, QPixmap, QColor, QKeySequence, QImage
from PyQt5.QtCore import Qt, QPoint, QRect
from reportlab.pdfgen import canvas as rl_canvas


# -------------------- Page --------------------
class Page:
    def __init__(self, w=1000, h=700, bg_color=Qt.white, bg_image=None):
        self.canvas = QPixmap(w, h)
        self.canvas.fill(bg_color)
        self.bg_image = None
        if bg_image:
            self.set_background(bg_image)

    def set_background(self, image_path):
        # draw background scaled to fill
        bg = QPixmap(image_path).scaled(
            self.canvas.width(), self.canvas.height(),
            Qt.KeepAspectRatioByExpanding, Qt.SmoothTransformation
        )
        painter = QPainter(self.canvas)
        painter.drawPixmap(0, 0, bg)
        painter.end()
        self.bg_image = image_path

    def size(self):
        return self.canvas.width(), self.canvas.height()

    def clone(self):
        p = Page(*self.size())
        p.canvas = self.canvas.copy()
        p.bg_image = self.bg_image
        return p


# -------------------- Main App --------------------
class Notebook(QMainWindow):
    def __init__(self):
        super().__init__()
        self.setWindowTitle("Notebook")
        self.setGeometry(100, 100, 1200, 800)

        # drawing state
        self.drawing = False
        self.last_point = QPoint()
        self.pen_color = QColor(Qt.black)
        self.pen_width = 5
        self.eraser = False
        self.eyedropper = False

        # pasted / floating image state
        self.floating_image = None       # QPixmap
        self.floating_pos = QPoint(100, 100)
        self.move_mode = False           # ctrl+m toggles; when toggling off, we stamp into canvas

        # undo/redo stacks per page index
        self.undo_stacks = {}
        self.redo_stacks = {}

        # pages
        self.pages = []
        self.current_page_idx = -1

        # UI
        self.toolbar = None
        self.canvas_top = 0
        self.init_toolbar()
        self.init_right_panel()

        # first page sized to current inner area
        self.add_page()

        # shortcuts
        QShortcut(QKeySequence("Ctrl+,"), self, activated=self.toggle_eraser_shortcut)
        QShortcut(QKeySequence("Ctrl+."), self, activated=self.add_page_same_bg)
        QShortcut(QKeySequence("Ctrl+Z"), self, activated=self.undo)
        QShortcut(QKeySequence("Ctrl+Y"), self, activated=self.redo)
        QShortcut(QKeySequence("Ctrl+V"), self, activated=self.paste_image)
        QShortcut(QKeySequence("Ctrl+M"), self, activated=self.toggle_move_mode)

    # ---------- UI ----------
    def init_toolbar(self):
        tb = QToolBar("Tools")
        tb.setMovable(False)
        self.addToolBar(tb)
        self.toolbar = tb

        # color palette buttons
        palette = [
            "#000000", "#ffffff", "#ff0000", "#00a000",
            "#1e90ff", "#ff00ff", "#ffa500", "#8a2be2"
        ]
        tb.addWidget(QLabel(" Ink: "))
        for hexc in palette:
            btn = QPushButton()
            btn.setFixedSize(22, 22)
            btn.setStyleSheet(f"border:1px solid #888;background:{hexc};")
            btn.clicked.connect(lambda _, c=QColor(hexc): self.set_pen_color(c))
            tb.addWidget(btn)

        # color picker (eyedropper)
        pick_act = QAction("Pick Color", self)
        pick_act.setToolTip("Pick color from page")
        pick_act.triggered.connect(self.enable_eyedropper)
        tb.addAction(pick_act)

        # eraser
        self.eraser_act = QAction("Eraser (Ctrl+,)", self, checkable=True)
        self.eraser_act.triggered.connect(self.toggle_eraser)
        tb.addAction(self.eraser_act)

        # brush size
        tb.addWidget(QLabel("  Brush: "))
        self.slider = QSlider(Qt.Horizontal)
        self.slider.setMinimum(1)
        self.slider.setMaximum(60)
        self.slider.setValue(self.pen_width)
        self.slider.valueChanged.connect(lambda v: setattr(self, "pen_width", v))
        self.slider.setFixedWidth(160)
        tb.addWidget(self.slider)

        tb.addSeparator()

        # background image
        bg_act = QAction("Set Background Image", self)
        bg_act.triggered.connect(self.set_background_image)
        tb.addAction(bg_act)

        tb.addSeparator()

        # export PDF
        pdf_act = QAction("Export PDF (page size)", self)
        pdf_act.triggered.connect(self.export_pdf)
        tb.addAction(pdf_act)

    def init_right_panel(self):
        # right dock with page list + add/delete
        container = QWidget()
        v = QVBoxLayout(container)
        v.setContentsMargins(6, 6, 6, 6)

        self.page_list = QListWidget()
        self.page_list.currentRowChanged.connect(self.change_page)
        v.addWidget(self.page_list)

        row = QHBoxLayout()
        add_btn = QPushButton("Add")
        add_btn.clicked.connect(self.add_page_same_bg)
        del_btn = QPushButton("Delete")
        del_btn.clicked.connect(self.delete_current_page)
        row.addWidget(add_btn)
        row.addWidget(del_btn)
        v.addLayout(row)

        dock = QDockWidget("Pages", self)
        dock.setWidget(container)
        dock.setAllowedAreas(Qt.RightDockWidgetArea)
        self.addDockWidget(Qt.RightDockWidgetArea, dock)

    # ---------- Utility ----------
    def page(self):
        if 0 <= self.current_page_idx < len(self.pages):
            return self.pages[self.current_page_idx]
        return None

    def drawing_rect(self):
        # area where we draw (under toolbar), centered horizontally at x=0 for simplicity
        top = self.toolbar.geometry().height()
        self.canvas_top = top
        pg = self.page()
        if not pg:
            return QRect(0, top, self.width(), self.height() - top)
        w, h = pg.size()
        return QRect(0, top, w, h)

    def to_canvas_point(self, p):
        return QPoint(p.x(), p.y() - self.canvas_top)

    def in_canvas(self, p):
        if not self.page():
            return False
        w, h = self.page().size()
        return (0 <= p.x() < w) and (0 <= p.y() < h)

    # ---------- Undo/Redo ----------
    def ensure_stacks(self):
        i = self.current_page_idx
        self.undo_stacks.setdefault(i, [])
        self.redo_stacks.setdefault(i, [])

    def snapshot(self):
        """push current canvas into undo stack"""
        self.ensure_stacks()
        self.undo_stacks[self.current_page_idx].append(self.page().canvas.copy())
        # clear redo on new action
        self.redo_stacks[self.current_page_idx].clear()

    def undo(self):
        self.ensure_stacks()
        u = self.undo_stacks[self.current_page_idx]
        if not u:
            return
        # push current to redo, pop last undo into canvas
        current_copy = self.page().canvas.copy()
        last = u.pop()
        self.redo_stacks[self.current_page_idx].append(current_copy)
        self.page().canvas = last
        self.update()

    def redo(self):
        self.ensure_stacks()
        r = self.redo_stacks[self.current_page_idx]
        if not r:
            return
        current_copy = self.page().canvas.copy()
        next_img = r.pop()
        self.undo_stacks[self.current_page_idx].append(current_copy)
        self.page().canvas = next_img
        self.update()

    # ---------- Pages ----------
    def add_page(self):
        # create to fit current window content
        top = self.toolbar.geometry().height()
        w = max(200, self.width())
        h = max(200, self.height() - top)
        bg = self.page().bg_image if self.page() else None
        pg = Page(w, h, bg_image=bg)
        self.pages.append(pg)
        self.current_page_idx = len(self.pages) - 1
        self.update_page_list()
        self.ensure_stacks()
        self.snapshot()  # initial state

    def add_page_same_bg(self):
        self.add_page()

    def delete_current_page(self):
        if not self.pages:
            return
        idx = self.current_page_idx
        self.pages.pop(idx)
        self.undo_stacks.pop(idx, None)
        self.redo_stacks.pop(idx, None)
        if not self.pages:
            self.current_page_idx = -1
        else:
            self.current_page_idx = max(0, idx - 1)
        self.update_page_list()
        self.update()

    def change_page(self, idx):
        if 0 <= idx < len(self.pages):
            self.current_page_idx = idx
            self.update()

    def update_page_list(self):
        self.page_list.clear()
        for i in range(len(self.pages)):
            self.page_list.addItem(f"Page {i+1}")
        if self.current_page_idx >= 0:
            self.page_list.setCurrentRow(self.current_page_idx)

    # ---------- Actions ----------
    def set_pen_color(self, c: QColor):
        self.pen_color = c
        self.eraser = False
        self.eraser_act.setChecked(False)
        self.eyedropper = False

    def enable_eyedropper(self):
        self.eyedropper = True
        self.eraser = False
        self.eraser_act.setChecked(False)

    def toggle_eraser(self, checked):
        self.eraser = checked
        if checked:
            self.eyedropper = False

    def toggle_eraser_shortcut(self):
        self.eraser_act.setChecked(not self.eraser_act.isChecked())
        self.toggle_eraser(self.eraser_act.isChecked())

    def set_background_image(self):
        fname, _ = QFileDialog.getOpenFileName(self, "Choose Background Image", "", "Images (*.png *.jpg *.jpeg *.bmp)")
        if fname and self.page():
            self.snapshot()
            self.page().set_background(fname)
            self.update()

    def export_pdf(self):
        if not self.pages:
            return
        fname, _ = QFileDialog.getSaveFileName(self, "Save PDF", "", "PDF Files (*.pdf)")
        if not fname:
            return
        # Use reportlab, page size equals canvas pixel size (points); draw 1:1
        tmpdir = tempfile.mkdtemp(prefix="nbkpdf_")
        try:
            c = None
            for i, pg in enumerate(self.pages):
                img_path = os.path.join(tmpdir, f"pg_{i+1}.png")
                pg.canvas.save(img_path)
                w, h = pg.size()
                if c is None:
                    c = rl_canvas.Canvas(fname, pagesize=(w, h))
                else:
                    c.setPageSize((w, h))
                c.drawImage(img_path, 0, 0, width=w, height=h)
                c.showPage()
            if c:
                c.save()
            QMessageBox.information(self, "Exported", f"PDF saved: {fname}")
        finally:
            # cleanup temp images
            for f in os.listdir(tmpdir):
                try:
                    os.remove(os.path.join(tmpdir, f))
                except:
                    pass
            try:
                os.rmdir(tmpdir)
            except:
                pass

    def paste_image(self):
        cb = QApplication.clipboard()
        pm = cb.pixmap()
        if pm.isNull():
            img = cb.image()
            if img.isNull():
                QMessageBox.warning(self, "Paste", "Clipboard has no image.")
                return
            pm = QPixmap.fromImage(img)

        # create floating image centered
        if not self.page():
            return
        self.floating_image = pm
        w, h = self.page().size()
        fx = max(0, (w - pm.width()) // 2)
        fy = max(0, (h - pm.height()) // 2)
        self.floating_pos = QPoint(fx, fy)
        self.move_mode = True
        self.update()

    def toggle_move_mode(self):
        # If we have a floating image and move_mode is True -> stamp it into canvas and exit move mode.
        if self.floating_image is not None:
            if self.move_mode:
                # stamp
                self.snapshot()
                painter = QPainter(self.page().canvas)
                painter.drawPixmap(self.floating_pos, self.floating_image)
                painter.end()
                self.floating_image = None
                self.move_mode = False
                self.update()
            else:
                # re-enter move mode to reposition before stamping again
                self.move_mode = True
                self.update()

    # ---------- Events ----------
    def resizeEvent(self, event):
        # do not auto-rescale existing pages (keeps crisp drawings),
        # but new pages will take the current window size.
        super().resizeEvent(event)
        self.update()

    def paintEvent(self, event):
        painter = QPainter(self)
        rect = self.drawing_rect()
        pg = self.page()
        if pg:
            painter.drawPixmap(rect.topLeft(), pg.canvas)

        # draw floating (pasted) image overlay
        if self.floating_image is not None:
            overlay_pos = QPoint(rect.left() + self.floating_pos.x(),
                                 rect.top() + self.floating_pos.y())
            painter.setOpacity(0.95)
            painter.drawPixmap(overlay_pos, self.floating_image)
            painter.setOpacity(1.0)
            # optional outline while moving
            if self.move_mode:
                pen = QPen(Qt.DashLine)
                painter.setPen(pen)
                painter.drawRect(QRect(overlay_pos,
                                       self.floating_image.size()))

    def mousePressEvent(self, event):
        rect = self.drawing_rect()
        if event.button() == Qt.LeftButton:
            if self.floating_image is not None and self.move_mode:
                # start dragging floating image if clicked inside it
                local = event.pos() - rect.topLeft()
                rel = local - self.floating_pos
                inside = (0 <= rel.x() < self.floating_image.width() and
                          0 <= rel.y() < self.floating_image.height())
                if inside:
                    self.drawing = True  # reuse flag as dragging
                    self.last_point = local
                    return

            # normal drawing / eyedropper
            local = event.pos() - rect.topLeft()
            if not self.in_canvas(local):
                return

            if self.eyedropper and self.page():
                img = self.page().canvas.toImage()
                c = QColor(img.pixel(local))
                self.set_pen_color(c)
                self.eyedropper = False
                return

            if self.page() and self.floating_image is None and not self.move_mode:
                self.drawing = True
                self.last_point = local
                # snapshot only once per stroke
                self.snapshot()

    def mouseMoveEvent(self, event):
        rect = self.drawing_rect()
        if not self.page():
            return

        if self.floating_image is not None and self.move_mode and self.drawing:
            # dragging floating image
            local = event.pos() - rect.topLeft()
            delta = local - self.last_point
            self.floating_pos += delta
            # clamp to canvas
            w, h = self.page().size()
            self.floating_pos.setX(max(0, min(self.floating_pos.x(), w - self.floating_image.width())))
            self.floating_pos.setY(max(0, min(self.floating_pos.y(), h - self.floating_image.height())))
            self.last_point = local
            self.update()
            return

        if self.drawing and event.buttons() & Qt.LeftButton and self.floating_image is None and not self.move_mode:
            local = event.pos() - rect.topLeft()
            if not self.in_canvas(local):
                return
            painter = QPainter(self.page().canvas)
            color = QColor(Qt.white) if self.eraser else self.pen_color
            pen = QPen(color, self.pen_width, Qt.SolidLine, Qt.RoundCap, Qt.RoundJoin)
            painter.setRenderHint(QPainter.Antialiasing, True)
            painter.setPen(pen)
            painter.drawLine(self.last_point, local)
            painter.end()
            self.last_point = local
            self.update()

    def mouseReleaseEvent(self, event):
        if event.button() == Qt.LeftButton:
            self.drawing = False


# -------------------- Run --------------------
if __name__ == "__main__":
    app = QApplication(sys.argv)
    win = Notebook()
    win.show()
    sys.exit(app.exec_())
