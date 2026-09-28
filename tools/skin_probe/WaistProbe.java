import javafx.application.Platform;
import javafx.fxml.FXMLLoader;
import javafx.geometry.Point2D;
import javafx.geometry.Rectangle2D;
import javafx.scene.Node;
import javafx.scene.Parent;
import javafx.scene.Scene;
import javafx.scene.SnapshotParameters;
import javafx.scene.image.PixelReader;
import javafx.scene.image.WritableImage;
import javafx.scene.paint.Color;
import javafx.scene.shape.Polygon;
import javafx.scene.text.Text;
import javafx.scene.transform.Scale;

import javax.imageio.ImageIO;
import java.awt.image.BufferedImage;
import java.io.ByteArrayInputStream;
import java.io.File;
import java.nio.charset.StandardCharsets;
import java.util.ArrayList;
import java.util.List;
import java.util.Locale;
import java.util.concurrent.CountDownLatch;

/*
 * Пробник талий клапанов (tools/skin_waist_probe.py компилирует и запускает его
 * во временном каталоге). Рендерит ValveControl так, как его выгружает
 * canvas_to_fxml (prefWidth x prefHeight, orientation, valueVisible=false), в двух
 * корнях — Pane (геометрия таблицы) и AnchorPane (корень выгрузки; он округляет
 * pref-размер вверх до целого: 31.5 -> 32, 37.8 -> 38), и печатает по строке на
 * прогон:
 *   WAIST {"skin":..,"orient":..,"root":..,"W":..,"H":..,"frame":[x0,y0,x1,y1],
 *          "left":[[x,y],..],"right":[[x,y],..]}
 * frame — НОМИНАЛЬНАЯ рамка контрола (layout + W x H, как на холсте) в координатах
 * сцены (VERTICAL* — повёрнута вокруг центра: ширина H, высота W); left/right —
 * вершины #leftPoly/#rightPoly («бабочка»), их общая вершина — талия. Долю считает
 * python-обёртка.
 *
 * Аргументы: [--png=DIR] и группы по четыре — skin orient W H. С --png каждая
 * группа HORIZONTAL в корне Pane снимается в DIR/<skin>.png — PNG-скин «Ручной
 * правки»: только контрол, ровно номинальная рамка x10, прозрачный фон, поворот на
 * 90° по часовой (база редактора вертикальная, привод справа; так же VERTICAL
 * делает OrientationService — rotate(90) горизонтальной рамки). Снимается H, а не
 * VERTICAL: в снимке VERTICAL подпись значения видна вопреки valueVisible=false.
 * Код выхода: 0 — все группы отрендерены, 3 — исключение (стек в stderr).
 */
public class WaistProbe {
    static final double PNG_SCALE = 10.0;   // рабочие рамки x10 — целые пиксели
    static String pngDir = null;

    public static void main(String[] a) throws Exception {
        CountDownLatch done = new CountDownLatch(1);
        int[] rc = {0};
        final int from = (a.length > 0 && a[0].startsWith("--png=")) ? 1 : 0;
        if (from == 1) pngDir = a[0].substring(6);
        Platform.startup(() -> { });
        Platform.runLater(() -> {
            try {
                for (int i = from; i + 3 < a.length; i += 4) {
                    for (String root : new String[]{"Pane", "AnchorPane"}) {
                        one(a[i], a[i + 1], Double.parseDouble(a[i + 2]),
                            Double.parseDouble(a[i + 3]), root);
                    }
                }
            } catch (Throwable t) {
                t.printStackTrace();
                rc[0] = 3;
            } finally {
                done.countDown();
            }
        });
        done.await();
        Platform.exit();
        System.out.flush();
        System.exit(rc[0]);
    }

    static String f(double d) { return String.format(Locale.ROOT, "%.4f", d); }

    static void walk(Node n, List<Node> acc) {
        acc.add(n);
        if (n instanceof Parent) for (Node c : ((Parent) n).getChildrenUnmodifiable()) walk(c, acc);
    }

    static String pts(Polygon p) {
        StringBuilder s = new StringBuilder("[");
        List<Double> v = p.getPoints();
        for (int k = 0; k + 1 < v.size(); k += 2) {
            Point2D q = p.localToScene(v.get(k), v.get(k + 1));
            if (k > 0) s.append(",");
            s.append("[").append(f(q.getX())).append(",").append(f(q.getY())).append("]");
        }
        return s.append("]").toString();
    }

    static void one(String sk, String or, double w, double h, String rootTag) throws Exception {
        String fx = "<?xml version=\"1.0\" encoding=\"UTF-8\"?>"
            + "<?import javafx.scene.layout.*?><?import ru.get.common.controls.*?>"
            + "<" + rootTag + " xmlns=\"http://javafx.com/javafx/17\" xmlns:fx=\"http://javafx.com/fxml/1\""
            + " prefWidth=\"120\" prefHeight=\"120\"><children>"
            + "<ValveControl fx:id=\"v\" layoutX=\"30\" layoutY=\"30\" prefWidth=\"" + w + "\""
            + " prefHeight=\"" + h + "\" skinType=\"" + sk + "\" orientation=\"" + or + "\""
            + " valueVisible=\"false\"/></children></" + rootTag + ">";
        FXMLLoader loader = new FXMLLoader();
        Parent root = loader.load(new ByteArrayInputStream(fx.getBytes(StandardCharsets.UTF_8)));
        new Scene(root, 120, 120);
        root.applyCss(); root.layout(); root.applyCss(); root.layout();
        Node v = (Node) loader.getNamespace().get("v");
        // Номинальный центр, как на холсте. В Pane он совпадает с центром layoutBounds;
        // в AnchorPane контрол растянут до целого и повёрнут вокруг другого центра.
        double cx = 30 + w / 2, cy = 30 + h / 2;
        boolean vert = or.startsWith("VERTICAL");
        double fw = vert ? h : w, fh = vert ? w : h;
        Polygon left = null, right = null;
        List<Node> all = new ArrayList<>();
        walk(v, all);
        for (Node n : all) {
            if (!(n instanceof Polygon)) continue;
            if ("leftPoly".equals(n.getId())) left = (Polygon) n;
            if ("rightPoly".equals(n.getId())) right = (Polygon) n;
        }
        if (pngDir != null && "Pane".equals(rootTag) && "HORIZONTAL".equals(or)) {
            // Подпись значения («0%», «0») — Text прямо в корне скина. valueVisible=false
            // вне показанного окна её не прячет; в САПФИР её нет — прячем для снимка.
            for (Node c : ((Parent) v).getChildrenUnmodifiable())
                if (c instanceof Parent)
                    for (Node t : ((Parent) c).getChildrenUnmodifiable())
                        if (t instanceof Text) t.setVisible(false);
            png(v, cx - fw / 2, cy - fh / 2, fw, fh, new File(pngDir, sk + ".png"));
        }
        StringBuilder js = new StringBuilder("WAIST {\"skin\":\"").append(sk)
            .append("\",\"orient\":\"").append(or).append("\",\"root\":\"").append(rootTag)
            .append("\",\"W\":").append(f(w)).append(",\"H\":").append(f(h))
            .append(",\"frame\":[").append(f(cx - fw / 2)).append(",").append(f(cy - fh / 2)).append(",")
            .append(f(cx + fw / 2)).append(",").append(f(cy + fh / 2)).append("]")
            .append(",\"left\":").append(left == null ? "null" : pts(left))
            .append(",\"right\":").append(right == null ? "null" : pts(right)).append("}");
        System.out.println(js);
    }

    /** Снимок контрола в номинальной рамке x PNG_SCALE, повёрнутый на 90° по часовой. */
    static void png(Node v, double x0, double y0, double fw, double fh, File out) throws Exception {
        SnapshotParameters sp = new SnapshotParameters();
        sp.setFill(Color.TRANSPARENT);
        sp.setTransform(new Scale(PNG_SCALE, PNG_SCALE));
        int pw = (int) Math.round(fw * PNG_SCALE), ph = (int) Math.round(fh * PNG_SCALE);
        sp.setViewport(new Rectangle2D(x0 * PNG_SCALE, y0 * PNG_SCALE, pw, ph));
        WritableImage img = v.snapshot(sp, null);
        PixelReader pr = img.getPixelReader();
        BufferedImage bi = new BufferedImage(ph, pw, BufferedImage.TYPE_INT_ARGB);
        for (int y = 0; y < ph; y++) for (int x = 0; x < pw; x++) bi.setRGB(ph - 1 - y, x, pr.getArgb(x, y));
        if (!ImageIO.write(bi, "png", out)) throw new IllegalStateException("PNG не записан: " + out);
    }
}
