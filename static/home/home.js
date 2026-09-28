/* 外层壳：首页（环形功能入口）与功能视图之间的切换。
 *
 * 只做三件事：
 *   1. 把首页的圆形按钮排成一个环绕中心的圆环（直径与半径按可用空间算，保证不重叠、不越界）；
 *   2. 在「首页」与「功能页」两个视图之间切换，并地址栏 hash 同步（刷新 / 后退不丢位置）；
 *   3. 侧边栏的收起 / 展开。
 *
 * 不参与数据：各板块在 iframe 里整页加载，跨板块的「当前书籍」由 base.html 里的
 * window.EPUB_MEMORY 保管，本文件不碰它。
 */
(function () {
    'use strict';

    var app = document.getElementById('tkApp');
    var stage = document.getElementById('tkStage');
    var ring = document.getElementById('tkRing');
    var frame = document.getElementById('contentFrame');
    var homeNav = document.getElementById('tkHomeNav');
    var sidebarToggle = document.getElementById('tkSidebarToggle');
    var sidebarFab = document.getElementById('tkSidebarFab');

    var nodes = Array.prototype.slice.call(document.querySelectorAll('.home-node[data-url]'));
    var navItems = Array.prototype.slice.call(document.querySelectorAll('.nav-item[data-url]'));

    if (!app || !stage || !frame) {
        return;
    }

    /* ---------------- 环形排布 ---------------- */

    var MIN_NODE = 76;        // 圆形按钮直径下限
    var MAX_NODE = 150;       // 上限
    var MAX_RADIUS = 360;     // 环半径上限，避免超宽屏上拉得太散
    var NODE_GAP = 20;        // 相邻两个圆之间希望保留的空隙
    var EDGE = 12;            // 环上按钮与容器边缘的最小留白
    var AREA_RATIO = 0.22;    // 直径先按容器短边的这个比例取

    function layoutRing() {
        if (app.classList.contains('is-tool')) {
            return; // 首页隐藏时量不到尺寸，跳过
        }
        var count = nodes.length;
        var width = stage.clientWidth;
        var height = stage.clientHeight;
        if (!count || !width || !height) {
            return;
        }

        var minDim = Math.min(width, height);
        // 相邻圆心连线（弦）的长度系数：弦长 = 半径 × chord
        var chord = 2 * Math.sin(Math.PI / count);
        var size = Math.max(MIN_NODE, Math.min(MAX_NODE, Math.round(minDim * AREA_RATIO)));
        var radius = 0;
        var i;

        // 先把按钮放小到「排得下」为止：半径既不能越界，也不能让相邻按钮重叠。
        for (i = 0; i < 16; i++) {
            radius = Math.min(minDim / 2 - size / 2 - EDGE, MAX_RADIUS);
            var need = (size + NODE_GAP) / chord;
            if (radius >= need) {
                break;
            }
            var next = Math.max(MIN_NODE, size - Math.max(4, Math.ceil(need - radius)));
            if (next === size) {
                break; // 已到下限，按当前尺寸尽力排布
            }
            size = next;
        }
        if (!(radius > 0)) {
            radius = Math.max(40, minDim / 4);
        }

        stage.style.setProperty('--tk-node', size + 'px');

        var cx = width / 2;
        var cy = height / 2;
        var step = Math.PI * 2 / count;
        // 坐标不取整：取整会让各点到圆心的距离出现最多约 0.7px 的出入，环就会歪一点点。
        // 小数像素对渲染没有影响。
        nodes.forEach(function (node, index) {
            var angle = -Math.PI / 2 + step * index; // 从正上方开始，顺时针
            node.style.left = (cx + radius * Math.cos(angle)) + 'px';
            node.style.top = (cy + radius * Math.sin(angle)) + 'px';
        });

        if (ring) {
            ring.style.width = (radius * 2) + 'px';
            ring.style.height = (radius * 2) + 'px';
            ring.style.left = cx + 'px';
            ring.style.top = cy + 'px';
        }
    }

    /* ---------------- 视图切换 ---------------- */

    function showHome() {
        navItems.forEach(function (item) {
            item.classList.remove('active');
        });
        app.classList.add('is-home');
        app.classList.remove('is-tool');
        layoutRing(); // 首页刚变可见，此时才量得到尺寸
    }

    function openTool(item) {
        var url = item.dataset.url;
        if (!url) {
            return;
        }
        // 按 key 匹配而不是按元素相等，保证不同入口指向同一板块时一起高亮。
        navItems.forEach(function (nav) {
            nav.classList.toggle('active', nav.dataset.key === item.dataset.key);
        });
        // 同一板块不重复赋值 src，避免把已经加载好的页面重新加载一遍。
        if (frame.getAttribute('data-url') !== url) {
            frame.setAttribute('data-url', url);
            frame.src = url;
        }
        app.classList.add('is-tool');
        app.classList.remove('is-home');
    }

    /* ---------------- 侧边栏收起 / 展开 ---------------- */

    function applySidebar(collapsed) {
        app.classList.toggle('is-collapsed', collapsed);
        if (!sidebarToggle) {
            return;
        }
        sidebarToggle.textContent = collapsed ? '»' : '«';
        sidebarToggle.setAttribute('aria-expanded', collapsed ? 'false' : 'true');
        sidebarToggle.setAttribute('title', collapsed ? '展开导航栏' : '收起导航栏');
    }

    if (sidebarToggle) {
        sidebarToggle.addEventListener('click', function () {
            applySidebar(!app.classList.contains('is-collapsed'));
        });
    }

    // 收起后侧边栏内的按钮会随之隐藏，用左上角浮动按钮重新展开。
    if (sidebarFab) {
        sidebarFab.addEventListener('click', function () {
            applySidebar(false);
        });
    }

    /* ---------------- 地址栏 hash 同步 ---------------- */

    function findNav(key) {
        for (var i = 0; i < navItems.length; i++) {
            if (navItems[i].dataset.key === key) {
                return navItems[i];
            }
        }
        return null;
    }

    /* 单一入口：hash 变了就应用 hash，视图切换全部由它驱动。 */
    function applyHash() {
        var item = findNav(location.hash.replace(/^#/, ''));
        if (item) {
            openTool(item);
        } else {
            showHome();
        }
    }

    function goHome() {
        if (location.hash) {
            // 去掉 hash 并留下一条历史记录，于是「后退」能回到刚才的板块。
            history.pushState(null, '', location.pathname + location.search);
        }
        applyHash();
    }

    function goTool(key) {
        if (!key) {
            return;
        }
        if (location.hash === '#' + key) {
            applyHash(); // hash 没变就不会触发 hashchange，这里补一次
            return;
        }
        location.hash = key;
    }

    nodes.forEach(function (node) {
        node.addEventListener('click', function () {
            goTool(node.dataset.key);
        });
    });

    navItems.forEach(function (item) {
        item.addEventListener('click', function (event) {
            event.preventDefault();
            goTool(item.dataset.key);
        });
    });

    if (homeNav) {
        homeNav.addEventListener('click', function (event) {
            event.preventDefault();
            goHome();
        });
    }

    window.addEventListener('hashchange', applyHash);

    var pending = false;
    window.addEventListener('resize', function () {
        if (pending) {
            return;
        }
        pending = true;
        window.requestAnimationFrame(function () {
            pending = false;
            layoutRing();
        });
    });

    applySidebar(app.classList.contains('is-collapsed'));
    applyHash();
})();
