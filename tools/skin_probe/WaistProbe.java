import javafx.application.Platform;
import javafx.fxml.FXMLLoader;
import javafx.geometry.Bounds;
import javafx.geometry.Point2D;
import javafx.scene.Node;
import javafx.scene.Parent;
import javafx.scene.Scene;
import javafx.scene.shape.Polygon;

import java.io.ByteArrayInputStream;
import java.nio.charset.StandardCharsets;
import java.util.ArrayList;
import java.util.List;
import java.util.Locale;
import java.util.concurrent.CountDownLatch;

/*
 * Пробник талий клапанов (tools/skin_waist_probe.py компилирует и запускает его
 * во временном каталоге). Рендерит ValveControl так, как его выгружает
 * canvas_to_fxml (prefWidth x prefHeight, orientation, valueVisible=false), без
 * снимка, и печатает по строке на прогон:
 *   WAIST {"skin":..,"orient":..,"W":..,"H":..,"frame":[x0,y0,x1,y1],
 *          "left":[[x,y],..],"right":[[x,y],..]}
 * frame — видимая рамка контрола в координатах сцены (VERTICAL* — рамка повёрнута
 * вокруг центра: ширина H, высота W); left/right — вершины #leftPoly/#rightPoly
 * («бабочка»), их общая вершина — талия. Долю считает python-обёртка.
 *
 * Аргументы: группы по четыре — skin orient W H. Код выхода: 0 — все группы
 * отрендерены, 3 — исключение (стек в stderr).
 */
public class WaistProbe {
    public static void main(String[] a) throws Exception {
        CountDownLatch done = new CountDownLatch(1);
        int[] rc = {0};
        Platform.startup(() -> { });
        Platform.runLater(() -> {
            try {
                for (int i = 0; i + 3 < a.length; i += 4) {
                    one(a[i], a[i + 1], Double.parseDouble(a[i + 2]), Double.parseDouble(a[i + 3]));
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

    static void one(String sk, String or, double w, double h) throws Exception {
        String fx = "<?xml version=\"1.0\" encoding=\"UTF-8\"?>"
            + "<?import javafx.scene.layout.*?><?import ru.get.common.controls.*?>"
            + "<Pane xmlns=\"http://javafx.com/javafx/17\" xmlns:fx=\"http://javafx.com/fxml/1\""
            + " prefWidth=\"120\" prefHeight=\"120\"><children>"
            + "<ValveControl fx:id=\"v\" layoutX=\"30\" layoutY=\"30\" prefWidth=\"" + w + "\""
            + " prefHeight=\"" + h + "\" skinType=\"" + sk + "\" orientation=\"" + or + "\""
            + " valueVisible=\"false\"/></children></Pane>";
        FXMLLoader loader = new FXMLLoader();
        Parent root = loader.load(new ByteArrayInputStream(fx.getBytes(StandardCharsets.UTF_8)));
        new Scene(root, 120, 120);
        root.applyCss(); root.layout(); root.applyCss(); root.layout();
        Node v = (Node) loader.getNamespace().get("v");
        Bounds lb = v.localToScene(v.getLayoutBounds());
        double cx = lb.getMinX() + lb.getWidth() / 2, cy = lb.getMinY() + lb.getHeight() / 2;
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
        StringBuilder js = new StringBuilder("WAIST {\"skin\":\"").append(sk)
            .append("\",\"orient\":\"").append(or).append("\",\"W\":").append(f(w)).append(",\"H\":").append(f(h))
            .append(",\"frame\":[").append(f(cx - fw / 2)).append(",").append(f(cy - fh / 2)).append(",")
            .append(f(cx + fw / 2)).append(",").append(f(cy + fh / 2)).append("]")
            .append(",\"left\":").append(left == null ? "null" : pts(left))
            .append(",\"right\":").append(right == null ? "null" : pts(right)).append("}");
        System.out.println(js);
    }
}
